import os
import tempfile
import json
from flask import Flask, render_template, request, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
import openai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from gmail_utils import (
    get_gmail_service_flow, get_gmail_credentials_from_callback,
    get_credentials_from_refresh_token, # New import for refresh token handling
    get_gmail_service, list_messages, get_message_payload,
    classify_message, modify_message_labels,
    parse_unsubscribe_links, send_message,
    SCOPES # Ensure SCOPES is imported
)

app = Flask(__name__)
app.secret_key = os.urandom(24) # Ensure this is a strong, random key in production

UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = {'json'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

EMAILS_PER_PAGE = 45

OAUTH_CALLBACK_PATH = '/oauth2callback'

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def index():
    emails, logs = [], []
    form_data = {
        'openai_key': '',
        'summary_email': '',
        'vip_senders': '',
        'keywords': ''
    }

    filter_priority = request.args.get('filter', 'all').lower()
    page = request.args.get('page', 1)
    try:
        page = int(page)
        if page < 1:
            page = 1
    except ValueError:
        page = 1

    if 'page_tokens' not in session:
        session['page_tokens'] = [None]
    # 'gmail_credentials_data' will now store the full JSON string of credentials or None
    if 'gmail_credentials_data' not in session:
        session['gmail_credentials_data'] = None
    if 'gmail_oauth_state' not in session:
        session['gmail_oauth_state'] = None
    # 'uploaded_credentials_json_data' will store the client secrets JSON string from the form
    if 'uploaded_credentials_json_data' not in session:
        session['uploaded_credentials_json_data'] = None

    # --- Start of Authentication Logic ---
    creds = None
    auth_needed = False # Flag to determine if OAuth flow is required

    # 1. Try to load client secrets JSON from session (uploaded via form)
    client_secrets_json_data = session.get('uploaded_credentials_json_data')
    if not client_secrets_json_data:
        # If client secrets not yet uploaded, prompt user
        flash('Please upload your Google credentials.json (Web Application type) and provide OpenAI API key to start.', 'info')
        return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                               filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

    # 2. Try to get refresh token from environment variable
    refresh_token_env = os.environ.get('GOOGLE_REFRESH_TOKEN')
    if refresh_token_env:
        try:
            # Rebuild credentials from refresh token
            creds = get_credentials_from_refresh_token(refresh_token_env, client_secrets_json_data)
            # Ensure refresh token is still part of the credentials object after refresh
            if not creds.refresh_token:
                flash("Environment refresh token is invalid or expired. Re-authenticating with Google.", "warning")
                auth_needed = True # Force new auth flow
            else:
                # Refresh access token using the refresh token
                creds.refresh(Request())
                session['gmail_credentials_data'] = creds.to_json() # Save refreshed creds to session for current usage
                session.modified = True
                flash("Authenticated using stored refresh token.", "success")

        except Exception as e:
            flash(f"Failed to use GOOGLE_REFRESH_TOKEN: {str(e)}. Re-authenticating with Google.", "warning")
            auth_needed = True # Force new auth flow
            # Clear invalid refresh token from env if it causes consistent errors? (Optional: manual intervention needed)
    else:
        # No refresh token in environment, so we need to go through OAuth
        auth_needed = True

    # --- Handle POST request (Form submission for credentials and other data) ---
    if request.method == 'POST':
        credentials_file = request.files.get('credentials')
        if credentials_file and allowed_file(credentials_file.filename):
            client_secrets_json_data = credentials_file.read().decode('utf-8')
            session['uploaded_credentials_json_data'] = client_secrets_json_data # Update stored client secrets
            flash("Credentials uploaded.", "success")
        elif not client_secrets_json_data: # If no new file uploaded and none in session
             flash('Google credentials.json file is required!', 'danger')
             return redirect(request.url)
        elif credentials_file and not allowed_file(credentials_file.filename): # If file uploaded but invalid
            flash('Invalid file. Please upload a valid credentials.json', 'danger')
            return redirect(request.url)

        session['openai_key'] = request.form.get('openai_key', '').strip()
        session['summary_email'] = request.form.get('summary_email', '').strip()
        vip_input = request.form.get('vip_senders', '')
        session['vip_senders'] = [v.strip() for v in vip_input.split(',') if v.strip()]
        keywords_input = request.form.get('keywords', '')
        session['keywords'] = [k.strip() for k in keywords_input.split(',') if k.strip()]

        if not session['openai_key'] or not session['summary_email']:
            flash('OpenAI API key and Summary Email are required!', 'danger')
            form_data['openai_key'] = session['openai_key']
            form_data['summary_email'] = session['summary_email']
            form_data['vip_senders'] = vip_input
            form_data['keywords'] = keywords_input
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

        # If a POST occurs, and we need authentication, initiate it.
        if auth_needed:
            redirect_uri = request.url_root.rstrip('/') + OAUTH_CALLBACK_PATH
            try:
                authorization_url, state = get_gmail_service_flow(client_secrets_json_data, redirect_uri)
                session['gmail_oauth_state'] = state
                session['page_tokens'] = [None]
                session['gmail_credentials_data'] = None # Clear old creds
                flash('Redirecting to Google for authentication...', 'info')
                return redirect(authorization_url)
            except Exception as e:
                flash(f'Error initiating Google authentication: {str(e)}. Make sure your credentials.json is for "Web application" type and redirect URIs are configured correctly.', 'danger')
                form_data['openai_key'] = session['openai_key']
                form_data['summary_email'] = session['summary_email']
                form_data['vip_senders'] = vip_input
                form_data['keywords'] = keywords_input
                return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                       filter_priority=filter_priority, page=page, has_next=False, has_prev=False)
        else: # POST and auth_needed is False (meaning we have an environment refresh token)
            # Just reload the page to display results using the refreshed creds
            flash("Using persistent Google authentication.", "success")
            return redirect(url_for('index', filter=filter_priority, page=page))

    # --- End of Authentication Logic ---

    # For GET requests:
    form_data['openai_key'] = session.get('openai_key', '')
    form_data['summary_email'] = session.get('summary_email', '')
    form_data['vip_senders'] = ', '.join(session.get('vip_senders', []))
    form_data['keywords'] = ', '.join(session.get('keywords', []))

    # If we still need auth after GET, this means no refresh token in env, and no active OAuth flow.
    # The form itself will prompt for credentials.
    if auth_needed:
        flash('A Google authentication is required to proceed. Please submit the form.', 'warning')
        return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                               filter_priority=filter_priority, page=page, has_next=False, has_prev=False)


    # If we reached here, 'creds' should be available either from session (after callback)
    # or from GOOGLE_REFRESH_TOKEN env var (loaded at top of function).
    try:
        # Load credentials from session (should be populated if auth_needed was false, or after oauth2callback)
        if session.get('gmail_credentials_data') is None:
            # Fallback for unexpected state, or if session cleared, force auth.
            flash('Authentication state lost. Please re-authenticate.', 'danger')
            return redirect(url_for('index'))

        creds_json = json.loads(session['gmail_credentials_data'])
        creds = Credentials.from_authorized_user_info(creds_json, SCOPES)
        
        # Final check if credentials are valid, refresh if needed
        if not creds.valid:
            if creds.refresh_token:
                creds.refresh(Request())
                session['gmail_credentials_data'] = creds.to_json()
                session.modified = True
            else:
                flash('Google credentials expired and no refresh token available. Please re-authenticate.', 'danger')
                # Force re-authentication
                session.pop('gmail_credentials_data', None)
                session.pop('gmail_oauth_state', None)
                return redirect(url_for('index'))

        service = get_gmail_service(creds)
        openai.api_key = session.get('openai_key', '')

        # ... (rest of your email processing logic remains the same) ...

        keywords = session.get('keywords', [])
        query_string = None
        if keywords:
            def q_escape(w):
                if ' ' in w:
                    return f'"{w}"'
                return w
            query_string = ' OR '.join([q_escape(k) for k in keywords])

        label_ids = None if query_string else ['INBOX']

        current_page_token = None
        if page > 1 and page <= len(session['page_tokens']):
            current_page_token = session['page_tokens'][page - 1]
        elif page > len(session['page_tokens']):
            current_page_token = session['page_tokens'][-1]


        messages, next_gmail_page_token = list_messages(
            service,
            max_results=EMAILS_PER_PAGE,
            label_ids=label_ids,
            query=query_string,
            page_token=current_page_token
        )

        if not messages:
            emails = []
            logs.append('No emails found for current criteria.')
            has_next = False
            has_prev = page > 1
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page,
                                   has_next=has_next, has_prev=has_prev)

        if next_gmail_page_token and (page + 1) > len(session['page_tokens']):
            session['page_tokens'].append(next_gmail_page_token)
            session.modified = True

        detailed_emails = []
        for msg in messages:
            subject, sender, snippet, body, _ = get_message_payload(service, msg['id'])
            priority = classify_message(subject, sender, body, session.get('vip_senders', []), keywords or [])

            try:
                if priority == 'high':
                    modify_message_labels(service, msg['id'], labels_to_add=['IMPORTANT'])
                else:
                    modify_message_labels(service, msg['id'], labels_to_remove=['IMPORTANT'])
            except Exception as label_err:
                logs.append(f"Warning: Could not modify labels for message {msg['id']}: {label_err}")

            gmail_url = f"https://mail.google.com/mail/u/0/#all/{msg['id']}"
            links = parse_unsubscribe_links(body)

            detailed_emails.append({
                'id': msg['id'],
                'subject': subject,
                'sender': sender,
                'snippet': snippet,
                'priority': priority,
                'links': links,
                'gmail_url': gmail_url
            })

        if filter_priority in ('high', 'normal'):
            emails = [e for e in detailed_emails if e['priority'] == filter_priority]
        else:
            emails = detailed_emails

        has_prev = page > 1
        has_next = bool(next_gmail_page_token)

        logs.append(f"Showing page {page} - {len(emails)} emails (filter: {filter_priority.capitalize()})")
        if next_gmail_page_token:
            logs.append(f"Next Gmail page token received: {next_gmail_page_token[:10]}...")

    except openai.AuthenticationError:
        flash('Invalid OpenAI API Key. Please check your key.', 'danger')
        logs = []
        emails = []
        session.pop('openai_key', None)
        form_data['openai_key'] = ''
        has_next = False
        has_prev = False
    except Exception as e:
        flash(f'An error occurred: {str(e)}', 'danger')
        emails = []
        logs = []
        has_next = False
        has_prev = False

    return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                           filter_priority=filter_priority, page=page,
                           has_next=has_next, has_prev=has_prev)


@app.route(OAUTH_CALLBACK_PATH)
def oauth2callback():
    state = request.args.get('state')
    error = request.args.get('error')

    if error:
        flash(f'Google OAuth Error: {error}', 'danger')
        return redirect(url_for('index'))

    stored_state = session.pop('gmail_oauth_state', None)
    credentials_data_string = session.get('uploaded_credentials_json_data')

    if stored_state is None or stored_state != state or credentials_data_string is None:
        flash('OAuth state mismatch, credentials data missing, or session expired. Please try again.', 'danger')
        return redirect(url_for('index'))

    try:
        redirect_uri = request.url_root.rstrip('/') + OAUTH_CALLBACK_PATH
        
        credentials = get_gmail_credentials_from_callback(
            credentials_data_string,
            redirect_uri,
            request.url
        )

        session['gmail_credentials_data'] = credentials.to_json()
        session.modified = True

        flash('Successfully authenticated with Google! Please check your Render logs for the refresh token to set as an environment variable.', 'success')
        # Remove uploaded_credentials_json_data after successful authentication
        # You might want to keep this if the user wants to re-run the app with a different client ID later.
        # session.pop('uploaded_credentials_json_data', None)
        return redirect(url_for('index', filter=request.args.get('filter', 'all'), page=1))

    except Exception as e:
        flash(f'Error exchanging authorization code: {str(e)}', 'danger')
        return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5001)
