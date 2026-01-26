import os
import argparse
import requests
import sys

# --- Configuration ---
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
TRELLO_KEY = os.environ.get('TRELLO_API_KEY')
TRELLO_TOKEN = os.environ.get('TRELLO_API_TOKEN')
TRELLO_LIST_ID = os.environ.get('TRELLO_LIST_ID')
REPO = os.environ.get('GITHUB_REPOSITORY')  # e.g. "owner/repo"

if not all([GITHUB_TOKEN, TRELLO_KEY, TRELLO_TOKEN, TRELLO_LIST_ID, REPO]):
    print("Error: Missing required environment variables.")
    sys.exit(1)

API_BASE = "https://api.github.com"
TRELLO_BASE = "https://api.trello.com/1"

def get_gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

def fetch_all_invocations(url):
    """Fetch all pages from a GH paginated API."""
    results = []
    while url:
        resp = requests.get(url, headers=get_gh_headers())
        resp.raise_for_status()
        results.extend(resp.json())
        url = resp.links.get('next', {}).get('url')
    return results

def get_issue(issue_number):
    url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}"
    resp = requests.get(url, headers=get_gh_headers())
    resp.raise_for_status()
    return resp.json()

def get_all_open_issues():
    url = f"{API_BASE}/repos/{REPO}/issues?state=open&per_page=100"
    return fetch_all_invocations(url)

def get_issue_comments(issue_number):
    url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}/comments"
    return fetch_all_invocations(url)

def get_trello_cards_in_list():
    url = f"{TRELLO_BASE}/lists/{TRELLO_LIST_ID}/cards"
    params = {'key': TRELLO_KEY, 'token': TRELLO_TOKEN}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()

def create_trello_card(name, desc):
    url = f"{TRELLO_BASE}/cards"
    params = {
        'key': TRELLO_KEY, 
        'token': TRELLO_TOKEN,
        'idList': TRELLO_LIST_ID,
        'name': name,
        'desc': desc,
        'pos': 'top'
    }
    resp = requests.post(url, params=params)
    resp.raise_for_status()
    return resp.json()

def add_comment_to_trello(card_id, text):
    url = f"{TRELLO_BASE}/cards/{card_id}/actions/comments"
    params = {
        'key': TRELLO_KEY,
        'token': TRELLO_TOKEN,
        'text': text
    }
    resp = requests.post(url, params=params)
    resp.raise_for_status()

def process_issue(issue, existing_cards_map):
    # Safety Check: Skip Pull Requests (GitHub API treats PRs as issues)
    if 'pull_request' in issue:
        print(f"Skipping PR #{issue['number']}")
        return

    issue_num = issue['number']
    title = issue['title']
    body = issue.get('body') or ""
    author = issue['user']['login']
    html_url = issue['html_url']

    # Card Title Format: "Title (#123)"
    card_title = f"{title} (#{issue_num})"

    # Check if exists
    if card_title in existing_cards_map:
        print(f"Skipping Issue #{issue_num} (Already exists in Trello)")
        return

    print(f"Processing Issue #{issue_num}: {title}")
    
    # Construct Description
    desc = f"{body}\n\n---\n**Issue Details:**\n- **Link:** {html_url}\n- **Author:** {author}"
    
    # Create Card
    try:
        card = create_trello_card(card_title, desc)
        card_id = card['id']
        print(f" -> Created Trello Card: {card_id}")

        # Fetch and sync comments
        comments = get_issue_comments(issue_num)
        if comments:
            print(f" -> Syncing {len(comments)} comments...")
            for comment in comments:
                c_body = comment['body']
                c_user = comment['user']['login']
                c_text = f"**{c_user} wrote on GitHub:**\n\n{c_body}"
                add_comment_to_trello(card_id, c_text)
    except Exception as e:
        print(f"Error processing issue #{issue_num}: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--issue', type=int, help='Sync a specific Issue Number')
    parser.add_argument('--all', action='store_true', help='Sync ALL open issues')
    args = parser.parse_args()

    print("Fetching Trello Cards for duplication check...")
    trello_cards = get_trello_cards_in_list()
    # Create map of "Name" -> ID to detect duplicates
    # We match strictly on the specific format "Title (#Number)" to avoid false positives
    existing_map = {c['name']: c['id'] for c in trello_cards}
    print(f"Found {len(existing_map)} existing cards.")

    if args.issue:
        # Single Mode
        try:
            issue = get_issue(args.issue)
            process_issue(issue, existing_map)
        except Exception as e:
            print(f"Failed to fetch/process issue #{args.issue}: {e}")
            sys.exit(1)
            
    elif args.all:
        # Bulk Mode
        print("Fetching all open GitHub Issues...")
        all_issues = get_all_open_issues()
        print(f"Found {len(all_issues)} open issues.")
        for issue in all_issues:
            process_issue(issue, existing_map)
    else:
        print("Usage: --issue <num> OR --all")
        sys.exit(1)

if __name__ == "__main__":
    main()
