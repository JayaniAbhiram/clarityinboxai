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
    get_gmail_service, list_messages, get_message_payload,
    classify_message, modify_message_labels,
    parse_unsubscribe_links, send_message
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
    if 'gmail_credentials_data' not in session:
        session['gmail_credentials_data'] = None
    # Ensure 'gmail_oauth_state' is initialized
    if 'gmail_oauth_state' not in session:
        session['gmail_oauth_state'] = None


    if request.method == 'POST':
        credentials_file = request.files.get('credentials')
        if not credentials_file or credentials_file.filename == '':
            flash('Google credentials.json file is required!', 'danger')
            return redirect(request.url)

        if not allowed_file(credentials_file.filename):
            flash('Invalid file. Please upload a valid credentials.json', 'danger')
            return redirect(request.url)

        credentials_data = credentials_file.read().decode('utf-8')
        # Store the raw credentials JSON string in session
        session['uploaded_credentials_json_data'] = credentials_data

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

        redirect_uri = request.url_root.rstrip('/') + OAUTH_CALLBACK_PATH

        try:
            # get_gmail_service_flow now returns only authorization_url and state string
            authorization_url, state = get_gmail_service_flow(credentials_data, redirect_uri)
            session['gmail_oauth_state'] = state # Store only the state string
            session['page_tokens'] = [None]
            session['gmail_credentials_data'] = None

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

    form_data['openai_key'] = session.get('openai_key', '')
    form_data['summary_email'] = session.get('summary_email', '')
    form_data['vip_senders'] = ', '.join(session.get('vip_senders', []))
    form_data['keywords'] = ', '.join(session.get('keywords', []))

    if session.get('gmail_credentials_data') is None:
        flash('Please upload your Google credentials.json (Web Application type) and provide OpenAI API key to start.', 'info')
        return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                               filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

    try:
        creds_json = json.loads(session['gmail_credentials_data'])
        creds = Credentials.from_authorized_user_info(creds_json, SCOPES)

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            session['gmail_credentials_data'] = creds.to_json()
            session.modified = True

        service = get_gmail_service(creds)
        openai.api_key = session.get('openai_key', '')

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
    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        flash(f'Google OAuth Error: {error}', 'danger')
        return redirect(url_for('index'))

    # Retrieve stored state and credentials data from session
    stored_state = session.pop('gmail_oauth_state', None) # Pop to clear state after use
    credentials_data_string = session.get('uploaded_credentials_json_data') # Get the raw JSON string

    if stored_state is None or stored_state != state or credentials_data_string is None:
        flash('OAuth state mismatch, credentials data missing, or session expired. Please try again.', 'danger')
        return redirect(url_for('index'))

    try:
        redirect_uri = request.url_root.rstrip('/') + OAUTH_CALLBACK_PATH
        
        # Pass the raw credentials data string and the full redirect_uri
        credentials = get_gmail_credentials_from_callback(
            credentials_data_string,
            redirect_uri,
            request.url # This contains the authorization response
        )

        session['gmail_credentials_data'] = credentials.to_json()
        session.modified = True

        flash('Successfully authenticated with Google!', 'success')
        # Remove uploaded_credentials_json_data after successful authentication
        session.pop('uploaded_credentials_json_data', None)
        return redirect(url_for('index', filter=request.args.get('filter', 'all'), page=1))

    except Exception as e:
        flash(f'Error exchanging authorization code: {str(e)}', 'danger')
        return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5001)
