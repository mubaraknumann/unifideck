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

# --- Signatures to detect source and prevent loops ---
SIG_GH_ON_TRELLO_PREFIX = "**" # Matches "**User wrote on GitHub:**"
SIG_TRELLO_ON_GH_PREFIX = "**[Trello]" # Matches "**[Trello] User wrote:**"

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

# --- GitHub API Helpers ---

def get_issue(issue_number):
    url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}"
    resp = requests.get(url, headers=get_gh_headers())
    resp.raise_for_status()
    return resp.json()

def get_all_open_issues():
    url = f"{API_BASE}/repos/{REPO}/issues?state=open&per_page=100"
    issues = fetch_all_invocations(url)
    issues.sort(key=lambda x: x['number'])
    return issues

def get_issue_comments(issue_number):
    url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}/comments"
    return fetch_all_invocations(url)

def add_comment_to_github(issue_number, text):
    url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}/comments"
    resp = requests.post(url, headers=get_gh_headers(), json={'body': text})
    resp.raise_for_status()
    return resp.json()

# --- Trello API Helpers ---

def get_trello_cards_in_list():
    url = f"{TRELLO_BASE}/lists/{TRELLO_LIST_ID}/cards"
    params = {'key': TRELLO_KEY, 'token': TRELLO_TOKEN}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()

def get_trello_comments(card_id):
    """Fetch comments (actions) for a card."""
    url = f"{TRELLO_BASE}/cards/{card_id}/actions"
    params = {'key': TRELLO_KEY, 'token': TRELLO_TOKEN, 'filter': 'commentCard'}
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

# --- Logic ---

def sync_card(issue, card_id):
    """Syncs comments bidirectional between Issue and Card."""
    issue_num = issue['number']
    print(f" -> Syncing comments for Issue #{issue_num} <-> Card {card_id}")

    # 1. Fetch all comments from both sides
    gh_comments = get_issue_comments(issue_num)
    trello_actions = get_trello_comments(card_id)

    # Convert Trello actions to a list of existing comment texts for dedup steps
    # Trello returns newest first; we reverse to process oldest -> newest if needed,
    # but for sets/existence checks order doesn't matter much.
    trello_texts = [action['data']['text'] for action in trello_actions]
    gh_texts = [c['body'] for c in gh_comments]

    # --- Sync GitHub -> Trello ---
    for gh_c in gh_comments:
        user = gh_c['user']['login']
        body = gh_c['body']
        
        # Check Signature: If this GH comment was originally from Trello, SKIP it.
        # (Prevents looping: Trello -> GH -> Trello)
        if body.startswith(SIG_TRELLO_ON_GH_PREFIX):
            continue

        # Format what the Trello comment SHOULD look like
        target_text = f"**{user} wrote on GitHub:**\n\n{body}"

        # Dedup: If this specific text already exists on Trello, skip
        if target_text in trello_texts:
            continue
        
        print(f"    [GH->Trello] New comment from {user}")
        add_comment_to_trello(card_id, target_text)

    # --- Sync Trello -> GitHub ---
    for action in trello_actions:
        data = action['data']
        text = data['text']
        creator = action['memberCreator']['fullName'] # or 'username'
        
        # Check Signature: If this Trello comment was originally from GitHub, SKIP it.
        if text.startswith(SIG_GH_ON_TRELLO_PREFIX) and "wrote on GitHub:**" in text:
            continue

        # Format what the GH comment SHOULD look like
        target_gh_text = f"**[Trello] {creator} wrote:**\n\n{text}"

        # Dedup: If this specific text already exists on GitHub, skip
        if target_gh_text in gh_texts:
            continue

        print(f"    [Trello->GH] New comment from {creator}")
        add_comment_to_github(issue_num, target_gh_text)

def process_issue(issue, existing_cards_map):
    if 'pull_request' in issue:
        return

    issue_num = issue['number']
    title = issue['title']
    card_title = f"{title} (#{issue_num})"

    if card_title in existing_cards_map:
        # Card exists: Just sync comments
        card_id = existing_cards_map[card_title]
        sync_card(issue, card_id)
    else:
        # Card missing: Create it, then sync comments
        print(f"Processing Issue #{issue_num}: Creating new card")
        body = issue.get('body') or ""
        author = issue['user']['login']
        html_url = issue['html_url']
        desc = f"{body}\n\n---\n**Issue Details:**\n- **Link:** {html_url}\n- **Author:** {author}"
        
        try:
            card = create_trello_card(card_title, desc)
            card_id = card['id']
            # After creation, perform sync to pull any existing GH comments
            sync_card(issue, card_id)
        except Exception as e:
            print(f"Error creating card for #{issue_num}: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--issue', type=int, help='Sync a specific Issue Number')
    parser.add_argument('--all', action='store_true', help='Sync ALL open issues')
    args = parser.parse_args()

    print("Fetching Trello Cards...")
    trello_cards = get_trello_cards_in_list()
    existing_map = {c['name']: c['id'] for c in trello_cards}

    if args.issue:
        try:
            issue = get_issue(args.issue)
            process_issue(issue, existing_map)
        except Exception as e:
            print(f"Failed to process #{args.issue}: {e}")
            sys.exit(1)
    elif args.all:
        print("Fetching open GitHub Issues...")
        all_issues = get_all_open_issues()
        for issue in all_issues:
            process_issue(issue, existing_map)
    else:
        print("Usage: --issue <num> OR --all")
        sys.exit(1)

if __name__ == "__main__":
    main()
