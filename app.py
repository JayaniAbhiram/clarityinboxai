import os
import tempfile
from flask import Flask, render_template, request, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
import openai
from gmail_utils import (
    get_gmail_service, list_messages, get_message_payload,
    classify_message, modify_message_labels,
    parse_unsubscribe_links, send_message
)

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Replace with your own secure secret key for production

UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = {'json'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

EMAILS_PER_PAGE = 45

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def index():
    emails, logs = [], []
    # Default form data
    form_data = {
        'openai_key': '',
        'summary_email': '',
        'vip_senders': '',
        'keywords': ''
    }

    # Retrieve filter and page from query parameters
    filter_priority = request.args.get('filter', 'all').lower()
    page = request.args.get('page', 1)
    try:
        page = int(page)
        if page < 1:
            page = 1
    except ValueError:
        page = 1

    # Initialize pagination tokens list in session if not present
    if 'page_tokens' not in session:
        session['page_tokens'] = [None] # First element is always None for the initial page

    if request.method == 'POST':
        # Handle file upload and form data submission
        if 'credentials' not in request.files:
            flash('Google credentials.json file is required!', 'danger')
            return redirect(request.url)

        file = request.files['credentials']
        if file.filename == '':
            flash('No file selected for credentials.json!', 'danger')
            return redirect(request.url)

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            cred_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(cred_path)
            session['credentials_json'] = cred_path

            # Delete old token.json to force re-authentication with new credentials
            token_path = os.path.join(tempfile.gettempdir(), 'token.json')
            if os.path.exists(token_path):
                os.remove(token_path)

            # Clear page tokens when new credentials are uploaded or run is initiated
            session['page_tokens'] = [None]
            page = 1 # Reset to first page
            flash('Credentials uploaded and session reset. Running email processing...', 'success')

        else:
            flash('Invalid file. Please upload a valid credentials.json', 'danger')
            return redirect(request.url)

        # Save other inputs to session for persistence
        session['openai_key'] = request.form.get('openai_key', '').strip()
        session['summary_email'] = request.form.get('summary_email', '').strip()

        vip_input = request.form.get('vip_senders', '')
        session['vip_senders'] = [v.strip() for v in vip_input.split(',') if v.strip()]

        keywords_input = request.form.get('keywords', '')
        session['keywords'] = [k.strip() for k in keywords_input.split(',') if k.strip()]

        # Save form inputs to repopulate form after POST
        form_data['openai_key'] = session['openai_key']
        form_data['summary_email'] = session['summary_email']
        form_data['vip_senders'] = vip_input
        form_data['keywords'] = keywords_input

        if not session['openai_key'] or not session['summary_email']:
            flash('OpenAI API key and Summary Email are required!', 'danger')
            return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                                   filter_priority=filter_priority, page=page,
                                   has_next=False, has_prev=False)

        # Redirect to GET request to process emails and display results
        return redirect(url_for('index', filter=filter_priority, page=page))

    # For GET requests (initial load or after POST redirect)
    # Populate form data from session
    form_data['openai_key'] = session.get('openai_key', '')
    form_data['summary_email'] = session.get('summary_email', '')
    form_data['vip_senders'] = ', '.join(session.get('vip_senders', []))
    form_data['keywords'] = ', '.join(session.get('keywords', []))

    if 'credentials_json' not in session:
        # Credentials not uploaded yet, just render empty page
        flash('Please upload your Google credentials.json and provide OpenAI API key to start.', 'info')
        return render_template('index.html', emails=emails, logs=logs, form_data=form_data,
                               filter_priority=filter_priority, page=page,
                               has_next=False, has_prev=False)

    try:
        openai.api_key = session.get('openai_key', '')
        service = get_gmail_service(session['credentials_json'])

        keywords = session.get('keywords', [])
        query_string = None
        if keywords:
            def q_escape(w):
                if ' ' in w:
                    return f'"{w}"'
                return w
            query_string = ' OR '.join([q_escape(k) for k in keywords])

        label_ids = None if query_string else ['INBOX']

        # Determine the page token to use for the current request
        current_page_token = None
        if page > 1 and page <= len(session['page_tokens']):
            current_page_token = session['page_tokens'][page - 1]
        elif page > len(session['page_tokens']):
            # This handles cases where user tries to jump too far ahead or
            # directly accesses a page beyond what we've fetched.
            # We'll just try to fetch the next available token.
            # This might require fetching previous pages internally to get the token.
            # For simplicity, if we don't have the token, we'll try to get it.
            # A more robust solution for arbitrary page jumps might be needed for very large datasets.
            current_page_token = session['page_tokens'][-1] # Use the last known token to fetch more

        # Fetch messages for the current page using Gmail's pageToken
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

        # Update page_tokens list if we've moved to a new 'next' page
        if next_gmail_page_token and (page + 1) > len(session['page_tokens']):
            # If we're on page 'N' and we get a token for 'N+1', add it.
            session['page_tokens'].append(next_gmail_page_token)
            session.modified = True # Important for Flask sessions when modifying a mutable object

        # Process messages
        detailed_emails = []
        for msg in messages:
            subject, sender, snippet, body, _ = get_message_payload(service, msg['id'])
            priority = classify_message(subject, sender, body, session.get('vip_senders', []), keywords or [])

            # Modify labels accordingly (ensure 'IMPORTANT' label exists in Gmail or handle error)
            try:
                if priority == 'high':
                    modify_message_labels(service, msg['id'], labels_to_add=['IMPORTANT'])
                else:
                    # Only try to remove if it's currently marked as IMPORTANT to avoid API errors
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

        # Filter by priority dropdown (only apply to the 20 messages fetched for the current page)
        if filter_priority in ('high', 'normal'):
            emails = [e for e in detailed_emails if e['priority'] == filter_priority]
        else:
            emails = detailed_emails

        # Determine pagination buttons visibility based on Gmail's nextPageToken
        has_prev = page > 1
        has_next = bool(next_gmail_page_token) # If next_gmail_page_token exists, there's a next page

        # If after filtering, we have no emails, and we know there's a next page from Gmail API
        # but our current page is empty due to filter, we might need a more complex redirect
        # For simplicity, we'll just show what we have.

        # If the number of filtered emails is less than EMAILS_PER_PAGE, it means
        # there are no more "next" pages for this filter from the current API token.
        # This is a nuance because Gmail API's next page token refers to the overall list,
        # not necessarily the filtered list.
        # For true filtered pagination, you might need to fetch more and apply filter until you fill the page,
        # or rely entirely on Gmail's 'q' parameter if it supports your specific filter logic.
        # For now, `has_next` purely reflects Gmail's `nextPageToken`.

        logs.append(f"Showing page {page} - {len(emails)} emails (filter: {filter_priority.capitalize()})")
        if next_gmail_page_token:
            logs.append(f"Next Gmail page token received: {next_gmail_page_token[:10]}...") # Show a snippet

    except openai.AuthenticationError:
        flash('Invalid OpenAI API Key. Please check your key.', 'danger')
        logs = [] # Clear logs on auth error
        emails = []
        # Clear sensitive data but keep other form_data
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


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5001)