import os
import json
import tempfile
from flask import Flask, render_template, request, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import openai
from gmail_utils import (
    list_messages, get_message_payload, classify_message,
    modify_message_labels, parse_unsubscribe_links, send_message
)

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Replace with a secure secret key for prod

UPLOAD_FOLDER = tempfile.gettempdir()
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'json'}
EMAILS_PER_PAGE = 20
OAUTH_CALLBACK_PATH = '/oauth2callback'

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def index():
    emails = []
    logs = []
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

    # Handle POST — start OAuth flow
    if request.method == 'POST':
        # Save OpenAI and other form data to session to persist between requests
        session['openai_key'] = request.form.get('openai_key', '').strip()
        session['summary_email'] = request.form.get('summary_email', '').strip()
        vip_input = request.form.get('vip_senders', '')
        session['vip_senders'] = [v.strip() for v in vip_input.split(',') if v.strip()]
        keywords_input = request.form.get('keywords', '')
        session['keywords'] = [k.strip() for k in keywords_input.split(',') if k.strip()]

        # Validate required inputs
        if not session['openai_key'] or not session['summary_email']:
            flash("OpenAI API Key and Summary Email are required!", "danger")
            # Repopulate form_data for rendering
            form_data['openai_key'] = session['openai_key']
            form_data['summary_email'] = session['summary_email']
            form_data['vip_senders'] = vip_input
            form_data['keywords'] = keywords_input
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

        # Validate and save credentials.json file
        credentials_file = request.files.get('credentials')
        if not credentials_file or credentials_file.filename == '':
            flash("Google credentials.json file is required to authenticate!", "danger")
            # Repopulate form_data for rendering
            form_data['openai_key'] = session['openai_key']
            form_data['summary_email'] = session['summary_email']
            form_data['vip_senders'] = vip_input
            form_data['keywords'] = keywords_input
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

        if not allowed_file(credentials_file.filename):
            flash("Invalid file. Please upload a credentials.json file.", "danger")
            # Repopulate form_data for rendering
            form_data['openai_key'] = session['openai_key']
            form_data['summary_email'] = session['summary_email']
            form_data['vip_senders'] = vip_input
            form_data['keywords'] = keywords_input
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

        try:
            cred_json = json.load(credentials_file)
            # Extract client_id and client_secret
            client_id = cred_json['web']['client_id']
            client_secret = cred_json['web']['client_secret']
            redirect_uri = url_for('oauth2callback', _external=True)

            # Save config in session
            session['client_config'] = {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "redirect_uris": [redirect_uri]
                }
            }

            flow = Flow.from_client_config(session['client_config'], SCOPES)
            flow.redirect_uri = redirect_uri

            authorization_url, state = flow.authorization_url(
                access_type='offline', include_granted_scopes='true'
            )
            session['state'] = state

            flash("Redirecting to Google for authentication...", "info")
            return redirect(authorization_url)

        except Exception as e:
            flash(f"Error processing credentials.json: {str(e)}", "danger")
            # Repopulate form_data for rendering
            form_data['openai_key'] = session['openai_key']
            form_data['summary_email'] = session['summary_email']
            form_data['vip_senders'] = vip_input
            form_data['keywords'] = keywords_input
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

    # GET Method: check if user has authenticated and has credentials
    credentials_json = session.get('credentials')
    if not credentials_json:
        # User has not authenticated yet — show form for credentials.json upload
        form_data['openai_key'] = session.get('openai_key', '')
        form_data['summary_email'] = session.get('summary_email', '')
        form_data['vip_senders'] = ', '.join(session.get('vip_senders', []))
        form_data['keywords'] = ', '.join(session.get('keywords', []))

        flash("Please upload your Google credentials.json and sign in.", "warning")
        return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                               filter_priority=filter_priority, page=page, has_next=False, has_prev=False)

    # User is authenticated — use stored credentials to access Gmail API
    try:
        creds = Credentials.from_authorized_user_info(json.loads(credentials_json), SCOPES)
        if not creds.valid:
            if creds.refresh_token:
                creds.refresh(Request())
                session['credentials'] = creds.to_json()
            else:
                flash("Google credentials expired; please re-authenticate.", "danger")
                session.pop('credentials', None)
                return redirect(url_for('index'))

        service = build('gmail', 'v1', credentials=creds)
        openai.api_key = session.get('openai_key', '')

        # Build Gmail API query string if keywords exist (search over mailbox)
        keywords = session.get('keywords', [])
        query_string = None
        if keywords:
            def q_escape(w):
                return f'"{w}"' if ' ' in w else w
            query_string = ' OR '.join(q_escape(k) for k in keywords)

        # Use label INBOX if no search query; else search entire mailbox
        label_ids = None if query_string else ['INBOX']

        # Note: Gmail API pagination tokens should ideally be used here for true paging,
        # but we'll fetch (page * EMAILS_PER_PAGE) emails and slice them ourselves for simplicity.
        max_fetch = page * EMAILS_PER_PAGE

        messages, _ = list_messages(
            service,
            max_results=max_fetch,
            label_ids=label_ids,
            query=query_string
        )

        if not messages:
            emails = []
            logs.append("No emails found matching the criteria.")
            has_next = False
            has_prev = page > 1
            return render_template('index.html', emails=emails, logs=logs, form_data=session,
                                   filter_priority=filter_priority, page=page, has_next=has_next, has_prev=has_prev)

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

        # Filter by priority
        if filter_priority in ('high', 'normal'):
            filtered_emails = [e for e in detailed_emails if e['priority'] == filter_priority]
        else:
            filtered_emails = detailed_emails

        start_idx = (page - 1) * EMAILS_PER_PAGE
        emails = filtered_emails[start_idx:start_idx + EMAILS_PER_PAGE]

        has_prev = page > 1
        has_next = len(filtered_emails) > start_idx + EMAILS_PER_PAGE

        logs.append(f"Showing page {page} with {len(emails)} emails (filter: {filter_priority.capitalize()})")

    except Exception as e:
        flash(f"Error fetching emails: {e}", "danger")
        emails = []
        logs = []
        has_prev = False
        has_next = False

    return render_template('index.html', emails=emails, logs=logs, form_data=session,
                           filter_priority=filter_priority, page=page,
                           has_next=has_next, has_prev=has_prev)

@app.route(OAUTH_CALLBACK_PATH)
def oauth2callback():
    state = session.get('state')
    incoming_state = request.args.get('state')
    error = request.args.get('error')

    if error:
        flash(f'Google OAuth error: {error}', 'danger')
        return redirect(url_for('index'))

    if state is None or incoming_state != state:
        flash('OAuth state mismatch or missing. Please try again.', 'danger')
        return redirect(url_for('index'))

    try:
        flow = Flow.from_client_config(session['client_config'], SCOPES, state=state)
        flow.redirect_uri = url_for('oauth2callback', _external=True)

        flow.fetch_token(authorization_response=request.url)

        creds = flow.credentials
        session['credentials'] = creds.to_json()

        flash('Successfully authenticated with Google!', 'success')
        return redirect(url_for('index'))

    except Exception as e:
        flash(f'Error retrieving credentials from OAuth callback: {str(e)}', 'danger')
        return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5001)
