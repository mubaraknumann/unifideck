import os
import argparse
import requests
import sys

# --- Configuration ---
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
TRELLO_KEY = os.environ.get('TRELLO_API_KEY')
TRELLO_TOKEN = os.environ.get('TRELLO_API_TOKEN')
TRELLO_LIST_ID = os.environ.get('TRELLO_LIST_ID')
REPO = os.environ.get('GITHUB_REPOSITORY')

if not all([GITHUB_TOKEN, TRELLO_KEY, TRELLO_TOKEN, TRELLO_LIST_ID, REPO]):
    print("Error: Missing required environment variables.")
    sys.exit(1)

API_BASE = "https://api.github.com"
TRELLO_BASE = "https://api.trello.com/1"

SIG_GH_ON_TRELLO_PREFIX = "**" 
SIG_TRELLO_ON_GH_PREFIX = "**[Trello]"

def get_gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

def fetch_all_invocations(url):
    results = []
    while url:
        resp = requests.get(url, headers=get_gh_headers())
        resp.raise_for_status()
        results.extend(resp.json())
        url = resp.links.get('next', {}).get('url')
    return results

# --- GitHub Helpers ---
def get_issue(issue_number):
    url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}"
    resp = requests.get(url, headers=get_gh_headers())
    resp.raise_for_status()
    return resp.json()

def get_all_issues():
    # Fetch ALL issues (open+closed) to ensure state sync works both ways
    url = f"{API_BASE}/repos/{REPO}/issues?state=all&per_page=100"
    issues = fetch_all_invocations(url)
    issues.sort(key=lambda x: x['number'])
    return issues

def get_issue_comments(issue_number):
    url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}/comments"
    return fetch_all_invocations(url)

def update_github_issue_state(issue_number, state):
    """state: 'open' or 'closed'"""
    url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}"
    resp = requests.patch(url, headers=get_gh_headers(), json={'state': state})
    resp.raise_for_status()
    print(f"    [Trello->GH] Issue #{issue_number} set to {state}")

def add_comment_to_github(issue_number, text):
    url = f"{API_BASE}/repos/{REPO}/issues/{issue_number}/comments"
    resp = requests.post(url, headers=get_gh_headers(), json={'body': text})
    resp.raise_for_status()
    return resp.json()

# --- Trello Helpers ---
def get_trello_lists_map():
    # Helper to map List ID -> List Name
    url = f"{TRELLO_BASE}/boards/{get_board_id_from_list()}/lists"
    params = {'key': TRELLO_KEY, 'token': TRELLO_TOKEN}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return {lst['id']: lst['name'] for lst in resp.json()}

def get_board_id_from_list():
    # Need to find Board ID first to get all lists
    url = f"{TRELLO_BASE}/lists/{TRELLO_LIST_ID}"
    params = {'key': TRELLO_KEY, 'token': TRELLO_TOKEN, 'fields': 'idBoard'}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()['idBoard']

def get_trello_cards_in_board():
    # Fetch all cards on board (not just list) to catch moved cars
    board_id = get_board_id_from_list()
    url = f"{TRELLO_BASE}/boards/{board_id}/cards"
    params = {'key': TRELLO_KEY, 'token': TRELLO_TOKEN, 'filter': 'all'}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()

def get_trello_actions(card_id):
    # Fetch comments, list moves, and dueComplete changes
    url = f"{TRELLO_BASE}/cards/{card_id}/actions"
    params = {
        'key': TRELLO_KEY, 
        'token': TRELLO_TOKEN, 
        'filter': 'commentCard,updateCard:idList,updateCard:dueComplete'
    }
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

def update_trello_card(card_id, params):
    url = f"{TRELLO_BASE}/cards/{card_id}"
    params['key'] = TRELLO_KEY
    params['token'] = TRELLO_TOKEN
    resp = requests.put(url, params=params)
    resp.raise_for_status()

def add_comment_to_trello(card_id, text):
    url = f"{TRELLO_BASE}/cards/{card_id}/actions/comments"
    params = {'key': TRELLO_KEY, 'token': TRELLO_TOKEN, 'text': text}
    requests.post(url, params=params)

# --- Core Logic ---

def sync_card(issue, card, lists_map):
    issue_num = issue['number']
    card_id = card['id']
    print(f" -> Syncing Issue #{issue_num} <-> Card {card_id}")

    # Find "Done" list ID (case-insensitive)
    done_list_id = None
    for lid, lname in lists_map.items():
        if lname.lower() == 'done':
            done_list_id = lid
            break

    # --- 1. State Sync (Current State Based - No History) ---
    is_gh_closed = (issue['state'] == 'closed')
    is_trello_complete = card['dueComplete']
    current_list_id = card['idList']

    # A. If GitHub is CLOSED but Trello is NOT complete -> Mark Trello complete + Move to Done
    if is_gh_closed and not is_trello_complete:
        updates = {'dueComplete': 'true'}
        print(f"    [GH->Trello] Issue closed -> Mark Trello complete")
        if done_list_id and current_list_id != done_list_id:
            updates['idList'] = done_list_id
            print(f"    [GH->Trello] Issue closed -> Move to Done list")
        update_trello_card(card_id, updates)
    
    # B. If Trello is COMPLETE but GitHub is OPEN -> Close GitHub
    elif is_trello_complete and not is_gh_closed:
        print(f"    [Trello->GH] Trello marked complete -> Close GH Issue")
        update_github_issue_state(issue_num, 'closed')
    
    # C. If GitHub is CLOSED, ensure card is in Done list (even if already complete)
    elif is_gh_closed and done_list_id and current_list_id != done_list_id:
        update_trello_card(card_id, {'idList': done_list_id})
        print(f"    [GH->Trello] Ensuring closed issue card is in Done list")
    
    # NOTE: We intentionally do NOT reopen GH if Trello is unchecked.
    # This prevents the flip-flop loop. To reopen, user must reopen on GH directly.

    # --- 2. Fetch History ---
    gh_comments = get_issue_comments(issue_num)
    trello_actions = get_trello_actions(card_id)

    trello_texts = [a['data']['text'] for a in trello_actions if 'text' in a['data']]
    gh_texts = [c['body'] for c in gh_comments]

    # --- 3. Sync Trello Actions -> GitHub ---
    # Trello actions are returned NEWEST FIRST. 
    # We only want the MOST RECENT action of each type.
    
    # A. MOVES: Find the most recent move action only
    latest_move = None
    for action in trello_actions:
        if action['type'] == 'updateCard' and 'listAfter' in action['data']:
            latest_move = action
            break  # First one is most recent
    
    if latest_move:
        creator = latest_move['memberCreator']['fullName']
        list_name = latest_move['data']['listAfter']['name']
        
        # Suppress "Moved to Done" if issue is already closed (programmatic move)
        if not (list_name.lower() == 'done' and is_gh_closed):
            move_sig = f"**[Trello] {creator} moved this card to list \"{list_name}\"**"
            if move_sig not in gh_texts:
                print(f"    [Trello->GH] Sync move to {list_name}")
                add_comment_to_github(issue_num, move_sig)
                gh_texts.append(move_sig)

    # B. COMPLETION: Already handled in Step 1 using current state.

    # C. COMMENTS: Sync all missing comments (comments are additive, not state)
    for action in trello_actions:
        if action['type'] == 'commentCard':
            creator = action['memberCreator']['fullName']
            text = action['data']['text']
            if text.startswith(SIG_GH_ON_TRELLO_PREFIX): continue
            
            target_text = f"**[Trello] {creator} wrote:**\n\n{text}"
            if target_text not in gh_texts:
                print(f"    [Trello->GH] New comment from {creator}")
                add_comment_to_github(issue_num, target_text)
                gh_texts.append(target_text)

    # --- 4. Sync GitHub Comments -> Trello ---
    for gh_c in gh_comments:
        user = gh_c['user']['login']
        body = gh_c['body']
        if body.startswith(SIG_TRELLO_ON_GH_PREFIX): continue

        target_text = f"**{user} wrote on GitHub:**\n\n{body}"
        if target_text not in trello_texts:
             print(f"    [GH->Trello] New comment from {user}")
             add_comment_to_trello(card_id, target_text)

def process_issue(issue, existing_cards_map, lists_map):
    if 'pull_request' in issue: return

    issue_num = issue['number']
    title = issue['title']
    card_title = f"{title} (#{issue_num})"

    if card_title in existing_cards_map:
        card = existing_cards_map[card_title]
        sync_card(issue, card, lists_map)
    else:
        # Card missing: Create it, then sync comments
        print(f"Processing Issue #{issue_num}: Creating new card")

        body = issue.get('body') or ""
        author = issue['user']['login']
        html_url = issue['html_url']
        desc = f"{body}\n\n---\n**Issue Details:**\n- **Link:** {html_url}\n- **Author:** {author}"
        
        try:
            card = create_trello_card(card_title, desc)
            sync_card(issue, card, lists_map)
        except Exception as e:
            print(f"Error creating card for #{issue_num}: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--issue', type=int, help='Sync a specific Issue Number')
    parser.add_argument('--all', action='store_true', help='Sync ALL issues')
    args = parser.parse_args()

    print("Fetching Trello Data...")
    lists_map = get_trello_lists_map()
    trello_cards = get_trello_cards_in_board()
    existing_map = {c['name']: c for c in trello_cards} # Map Name -> Full Card Object

    if args.issue:
        try:
            issue = get_issue(args.issue)
            process_issue(issue, existing_map, lists_map)
        except Exception as e:
            print(f"Failed to process #{args.issue}: {e}")
            sys.exit(1)
    elif args.all:
        print("Fetching all GitHub Issues...")
        all_issues = get_all_issues()
        for issue in all_issues:
            process_issue(issue, existing_map, lists_map)
    else:
        print("Usage: --issue <num> OR --all")
        sys.exit(1)

if __name__ == "__main__":
    main()
