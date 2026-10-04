#!/usr/bin/env python3
"""
Generate Xbox Disc-to-Digital & Xbox Play Anywhere Excel Master List
Fetches real-time community verification data from the XCT.live database.
"""

import urllib.request
import json
import time
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

EXCEL_PATH = "/home/deck/Downloads/Xbox_Disc_to_Digital_Play_Anywhere_Master_List.xlsx"

def fetch_all_games():
    print("Fetching community Disc-to-Digital database...")
    all_rows = []
    offset = 0
    limit = 500
    headers = {"User-Agent": "Mozilla/5.0"}

    while True:
        url = f"https://xct.live/api/v1/d2d/list?limit={limit}&offset={offset}"
        req = urllib.request.Request(url, headers=headers)
        try:
            data = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
            rows = data.get("rows", [])
            if not rows:
                break
            all_rows.extend(rows)
            offset += len(rows)
            total = data.get("total", 0)
            print(f"  Fetched {len(all_rows)} of {total} games...")
            if offset >= total:
                break
            time.sleep(0.3)
        except Exception as e:
            print(f"Error fetching offset {offset}: {e}")
            break

    print(f"Total games successfully retrieved: {len(all_rows)}")
    return all_rows

def style_header_cell(cell, text):
    cell.value = text
    cell.font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    cell.fill = PatternFill(start_color="107C10", end_color="107C10", fill_type="solid") # Xbox Green
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

def style_data_cell(cell, value, is_zebra=False, status=None, xpa=None):
    cell.value = value
    cell.font = Font(name="Segoe UI", size=10)
    
    # Status coloring overrides
    if status == "yes" or value in ("Yes", "Yes - Converts"):
        cell.fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
        cell.font = Font(name="Segoe UI", size=10, bold=True, color="276A3C")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    elif status == "no" or value in ("No", "No - Does Not Convert"):
        cell.fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
        cell.font = Font(name="Segoe UI", size=10, bold=True, color="C00000")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    elif status in ("unconfirmed", "variant", "retest"):
        cell.fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
        cell.font = Font(name="Segoe UI", size=10, bold=True, color="7F6000")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    elif is_zebra:
        cell.fill = PatternFill(start_color="F9FBF9", end_color="F9FBF9", fill_type="solid")
        cell.alignment = Alignment(vertical="center")
    else:
        cell.alignment = Alignment(vertical="center")

def autofit_columns(ws, max_cols=None):
    ws.views.sheetView[0].showGridLines = True
    thin_border = Border(
        left=Side(style='thin', color='E0E0E0'),
        right=Side(style='thin', color='E0E0E0'),
        top=Side(style='thin', color='E0E0E0'),
        bottom=Side(style='thin', color='E0E0E0')
    )
    
    for row in ws.iter_rows():
        for cell in row:
            if cell.row > 1:
                cell.border = thin_border

    cols_to_check = ws.columns if max_cols is None else list(ws.columns)[:max_cols]
    for col in cols_to_check:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col:
            val_str = str(cell.value or '')
            if cell.row == 1:
                max_len = max(max_len, len(val_str) + 4)
            else:
                max_len = max(max_len, min(len(val_str), 50))
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

def build_workbook(games):
    wb = openpyxl.Workbook()
    # Remove default sheet
    wb.remove(wb.active)

    # 1. Executive Summary Sheet
    ws_sum = wb.create_sheet(title="Executive Summary")
    ws_sum.views.sheetView[0].showGridLines = True
    
    # Title Header Banner
    ws_sum.merge_cells("A1:G1")
    title_cell = ws_sum["A1"]
    title_cell.value = "Xbox Disc-to-Digital & Xbox Play Anywhere (XPA) Master Intelligence"
    title_cell.font = Font(name="Segoe UI", size=16, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color="107C10", end_color="107C10", fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws_sum.row_dimensions[1].height = 40

    ws_sum.merge_cells("A2:G2")
    sub_cell = ws_sum["A2"]
    sub_cell.value = "Source: Community-verified tests from XCT.live & Xbox Play Anywhere Catalog • Updated October 2026"
    sub_cell.font = Font(name="Segoe UI", size=10, italic=True, color="555555")
    sub_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws_sum.row_dimensions[2].height = 22

    # Stats Calculation
    total_games = len(games)
    xpa_games = [g for g in games if g.get("xpa")]
    xpa_yes = [g for g in xpa_games if g.get("status") == "yes"]
    xpa_no = [g for g in xpa_games if g.get("status") == "no"]
    xpa_other = [g for g in xpa_games if g.get("status") not in ("yes", "no")]

    xpa_x1_upgrades = [g for g in xpa_yes if "Xbox One" in g.get("platforms", [])]
    xpa_xs_only = [g for g in xpa_yes if "Xbox One" not in g.get("platforms", [])]

    all_d2d_yes = [g for g in games if g.get("status") == "yes"]
    all_d2d_no = [g for g in games if g.get("status") == "no"]

    summary_data = [
        ("Metric / Category", "Count", "Percentage of XPA", "Key Context"),
        ("Total Tested Discs in Community Database", total_games, "—", "All physical Xbox One & Xbox Series X games tested"),
        ("Total Xbox Play Anywhere Titles Tracked", len(xpa_games), "100.0%", "Games that support Xbox + Windows PC cross-buy & cross-save"),
        ("XPA Discs that CONVERT to Digital (YES)", len(xpa_yes), f"{(len(xpa_yes)/len(xpa_games))*100:.1f}%", "Inserting disc successfully grants permanent digital entitlement + PC access"),
        ("  ↳ Buy Older Xbox One Disc -> Get Series X|S + PC", len(xpa_x1_upgrades), f"{(len(xpa_x1_upgrades)/len(xpa_games))*100:.1f}%", "CHEAPEST PATH: Buy used Xbox One disc, get native Series X|S upgrade + PC"),
        ("  ↳ Xbox Series X Disc Only (No Xbox One Disc)", len(xpa_xs_only), f"{(len(xpa_xs_only)/len(xpa_games))*100:.1f}%", "Current-gen only (e.g. Starfield, Baldur's Gate 3, Alan Wake 2, 007 First Light)"),
        ("XPA Discs that DO NOT CONVERT (NO)", len(xpa_no), f"{(len(xpa_no)/len(xpa_games))*100:.1f}%", "Blocked / Opted-out: e.g., ALL Forza games, Flight Sim, Square Enix, Bandai Namco"),
        ("Total Discs in DB Converting (All Games)", len(all_d2d_yes), f"{(len(all_d2d_yes)/total_games)*100:.1f}%", "General Disc-to-Digital compatibility across all tested publishers"),
        ("Total Discs in DB Failing Conversion", len(all_d2d_no), f"{(len(all_d2d_no)/total_games)*100:.1f}%", "Discs not participating in Microsoft's Disc-to-Digital licensing"),
    ]

    ws_sum.row_dimensions[4].height = 26
    for col_idx, text in enumerate(summary_data[0], 1):
        c = ws_sum.cell(row=4, column=col_idx)
        style_header_cell(c, text)

    for r_idx, row_vals in enumerate(summary_data[1:], 5):
        ws_sum.row_dimensions[r_idx].height = 22
        is_z = (r_idx % 2 == 0)
        for c_idx, val in enumerate(row_vals, 1):
            c = ws_sum.cell(row=r_idx, column=c_idx)
            c.value = val
            c.font = Font(name="Segoe UI", size=10, bold=(c_idx <= 2 or "↳" in str(row_vals[0])))
            if c_idx == 2:
                c.alignment = Alignment(horizontal="center", vertical="center")
            elif c_idx == 3:
                c.alignment = Alignment(horizontal="center", vertical="center")
            else:
                c.alignment = Alignment(vertical="center")
            if is_z:
                c.fill = PatternFill(start_color="F2F7F2", end_color="F2F7F2", fill_type="solid")

    # Insights & Important Notes
    notes_start = 15
    ws_sum.cell(row=notes_start, column=1, value="CRITICAL INSIGHTS FOR COLLECTORS & PC GAMERS").font = Font(name="Segoe UI", size=12, bold=True, color="107C10")
    
    insights = [
        "1. The 'Forza Exception': Every single Forza title (Forza Horizon 3, 4, 5, Forza Motorsport 7, Forza Motorsport 2023) is explicitly BLOCKED from Disc-to-Digital. The console returns: 'This game is not participating in digital licensing. You can still play with the disc inserted.' This is driven by expired car/music licensing and Turn 10/Playground Games policy.",
        "2. The Older Console Disc Hack (Xbox One -> Series X|S + PC): For 218 games, you do NOT need to buy expensive Series X discs. Buying a cheap secondhand Xbox One disc allows you to claim the digital license and instantly gain: (1) Xbox One version, (2) native Xbox Series X|S Smart Delivery upgrade, and (3) Windows PC Play Anywhere version.",
        "3. Xbox 360 Clarification: Xbox 360 discs do NOT qualify for Disc-to-Digital, nor do they support Play Anywhere (XPA was launched in late 2016 for Xbox One & PC). Backward compatibility for 360 discs still requires keeping the disc inserted in the console drive.",
        "4. Major Publisher Opt-Out Trends: Square Enix (Dragon Quest remakes, Visions of Mana, Outriders, Just Cause 4, Marvel's Avengers) and Bandai Namco (Dragon Ball Xenoverse 2, Scarlet Nexus, Katamari, Little Nightmares) systematically opt out of Disc-to-Digital.",
        "5. High-Value Working Titles: Microsoft's core first-party catalog (Halo Infinite, Halo Wars 2, Gears 5, Gears of War 4, Gears Tactics, Sea of Thieves, State of Decay 2, Starfield, Psychonauts 2) and top partners like SEGA/Atlus (Persona 3 Reload, Persona 5 Royal, Like a Dragon: Infinite Wealth) FULLY WORK, instantly granting the PC version for free upon disc scan."
    ]

    for idx, insight in enumerate(insights, notes_start + 1):
        ws_sum.merge_cells(start_row=idx, start_column=1, end_row=idx, end_column=7)
        c = ws_sum.cell(row=idx, column=1, value=insight)
        c.font = Font(name="Segoe UI", size=9.5)
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws_sum.row_dimensions[idx].height = 30

    autofit_columns(ws_sum, 7)
    ws_sum.column_dimensions["A"].width = 38
    ws_sum.column_dimensions["B"].width = 14
    ws_sum.column_dimensions["C"].width = 20
    ws_sum.column_dimensions["D"].width = 65

    # 2. Sheet: XPA - Converts (YES)
    ws_yes = wb.create_sheet(title="XPA - Converts (YES)")
    headers_yes = [
        "Game Title", "D2D Status", "Play Anywhere", "Older Disc Eligible? (Xbox One Disc)", 
        "Publisher", "Developer", "Release Year", "Supported Platforms", "Edition", "Region", "Verified", "Community Notes", "Product ID", "XCT URL"
    ]
    ws_yes.row_dimensions[1].height = 28
    for col_idx, h in enumerate(headers_yes, 1):
        style_header_cell(ws_yes.cell(row=1, column=col_idx), h)

    # Sort xpa_yes alphabetically
    xpa_yes_sorted = sorted(xpa_yes, key=lambda x: x.get("title", "").lower())
    for r_idx, g in enumerate(xpa_yes_sorted, 2):
        ws_yes.row_dimensions[r_idx].height = 20
        is_z = (r_idx % 2 == 0)
        platforms_list = g.get("platforms", [])
        platforms = ", ".join(platforms_list)
        has_x1 = "Xbox One" in platforms_list
        x1_label = "Yes (Xbox One Disc gives Series X|S + PC)" if has_x1 else "No (Series X Disc Only)"
        xct_url = f"https://xct.live/game/{g.get('tid')}" if g.get("tid") else ""
        
        row_vals = [
            (g.get("title") or "", False, None),
            ("Yes - Converts", False, "yes"),
            ("Yes", False, "yes"),
            (x1_label, False, "yes" if has_x1 else "unconfirmed"),
            (g.get("publisher") or "", is_z, None),
            (g.get("developer") or "", is_z, None),
            (g.get("year") or "", is_z, None),
            (platforms, is_z, None),
            (g.get("edition") or "Standard", is_z, None),
            (g.get("region") or "Global", is_z, None),
            ("Verified" if g.get("xctUserVerified") else "Reported", is_z, None),
            (g.get("note") or "", is_z, None),
            (g.get("productId") or "", is_z, None),
            (xct_url, is_z, None),
        ]
        for c_idx, (v, z, st) in enumerate(row_vals, 1):
            cell = ws_yes.cell(row=r_idx, column=c_idx)
            style_data_cell(cell, v, is_zebra=z, status=st)
            if c_idx in (7, 9, 10, 11):
                cell.alignment = Alignment(horizontal="center", vertical="center")

    ws_yes.freeze_panes = "A2"
    ws_yes.auto_filter.ref = f"A1:{get_column_letter(len(headers_yes))}{len(xpa_yes_sorted)+1}"
    autofit_columns(ws_yes)

    # 3. Sheet: XPA - Buy Xbox One Disc (218)
    ws_x1 = wb.create_sheet(title="XPA - Buy Xbox One Disc (218)")
    headers_x1 = [
        "Game Title", "Older Disc Path", "Smart Delivery Upgrade", "Play Anywhere (PC)", 
        "D2D Status", "Publisher", "Developer", "Release Year", "Edition", "Region", "Community Notes", "Product ID", "XCT URL"
    ]
    ws_x1.row_dimensions[1].height = 28
    for col_idx, h in enumerate(headers_x1, 1):
        style_header_cell(ws_x1.cell(row=1, column=col_idx), h)

    xpa_x1_sorted = sorted(xpa_x1_upgrades, key=lambda x: x.get("title", "").lower())
    for r_idx, g in enumerate(xpa_x1_sorted, 2):
        ws_x1.row_dimensions[r_idx].height = 20
        is_z = (r_idx % 2 == 0)
        xct_url = f"https://xct.live/game/{g.get('tid')}" if g.get("tid") else ""
        
        row_vals = [
            (g.get("title") or "", False, None),
            ("Xbox One Disc -> Series X|S + PC", False, "yes"),
            ("Yes (Free Series X|S Upgrade)", False, "yes"),
            ("Yes (Free PC Version)", False, "yes"),
            ("Yes - Converts", False, "yes"),
            (g.get("publisher") or "", is_z, None),
            (g.get("developer") or "", is_z, None),
            (g.get("year") or "", is_z, None),
            (g.get("edition") or "Standard", is_z, None),
            (g.get("region") or "Global", is_z, None),
            (g.get("note") or "", is_z, None),
            (g.get("productId") or "", is_z, None),
            (xct_url, is_z, None),
        ]
        for c_idx, (v, z, st) in enumerate(row_vals, 1):
            cell = ws_x1.cell(row=r_idx, column=c_idx)
            style_data_cell(cell, v, is_zebra=z, status=st)
            if c_idx in (8, 9, 10):
                cell.alignment = Alignment(horizontal="center", vertical="center")

    ws_x1.freeze_panes = "A2"
    ws_x1.auto_filter.ref = f"A1:{get_column_letter(len(headers_x1))}{len(xpa_x1_sorted)+1}"
    autofit_columns(ws_x1)

    # 4. Sheet: XPA - Does NOT Convert (NO)
    ws_no = wb.create_sheet(title="XPA - Does NOT Convert (NO)")
    headers_no = [
        "Game Title", "D2D Status", "Play Anywhere", "Publisher", "Developer", 
        "Release Year", "Specific Rejection Reason / Community Note", "Edition", "Region", "Product ID", "XCT URL"
    ]
    ws_no.row_dimensions[1].height = 28
    for col_idx, h in enumerate(headers_no, 1):
        style_header_cell(ws_no.cell(row=1, column=col_idx), h)

    xpa_no_sorted = sorted(xpa_no, key=lambda x: x.get("title", "").lower())
    for r_idx, g in enumerate(xpa_no_sorted, 2):
        ws_no.row_dimensions[r_idx].height = 22
        is_z = (r_idx % 2 == 0)
        xct_url = f"https://xct.live/game/{g.get('tid')}" if g.get("tid") else ""
        note = g.get("note") or "Game confirmed NOT participating in Disc to Digital licensing."
        
        row_vals = [
            (g.get("title") or "", False, None),
            ("No - Does Not Convert", False, "no"),
            ("Yes", False, "yes"),
            (g.get("publisher") or "", is_z, None),
            (g.get("developer") or "", is_z, None),
            (g.get("year") or "", is_z, None),
            (note, is_z, None),
            (g.get("edition") or "Standard", is_z, None),
            (g.get("region") or "Global", is_z, None),
            (g.get("productId") or "", is_z, None),
            (xct_url, is_z, None),
        ]
        for c_idx, (v, z, st) in enumerate(row_vals, 1):
            cell = ws_no.cell(row=r_idx, column=c_idx)
            style_data_cell(cell, v, is_zebra=z, status=st)
            if c_idx in (6, 8, 9):
                cell.alignment = Alignment(horizontal="center", vertical="center")

    ws_no.freeze_panes = "A2"
    ws_no.auto_filter.ref = f"A1:{get_column_letter(len(headers_no))}{len(xpa_no_sorted)+1}"
    autofit_columns(ws_no)
    ws_no.column_dimensions["G"].width = 50

    # 5. Sheet: All Play Anywhere Discs (338)
    ws_all_xpa = wb.create_sheet(title="All Play Anywhere Discs")
    headers_all_xpa = [
        "Game Title", "D2D Conversion Status", "Play Anywhere", "Older Disc Path (Xbox One Disc)", 
        "Publisher", "Developer", "Release Year", "Platforms", "Edition", "Region", "Notes / Rejection Reason", "Product ID", "XCT URL"
    ]
    ws_all_xpa.row_dimensions[1].height = 28
    for col_idx, h in enumerate(headers_all_xpa, 1):
        style_header_cell(ws_all_xpa.cell(row=1, column=col_idx), h)

    xpa_all_sorted = sorted(xpa_games, key=lambda x: (0 if x.get("status") == "yes" else 1, x.get("title", "").lower()))
    for r_idx, g in enumerate(xpa_all_sorted, 2):
        ws_all_xpa.row_dimensions[r_idx].height = 20
        is_z = (r_idx % 2 == 0)
        platforms_list = g.get("platforms", [])
        platforms = ", ".join(platforms_list)
        has_x1 = "Xbox One" in platforms_list
        x1_label = "Yes (Xbox One Disc eligible)" if has_x1 else "No (Series X Only)"
        xct_url = f"https://xct.live/game/{g.get('tid')}" if g.get("tid") else ""
        st = g.get("status", "").lower()
        st_label = "Yes - Converts" if st == "yes" else "No - Blocked" if st == "no" else st.capitalize()

        row_vals = [
            (g.get("title") or "", False, None),
            (st_label, False, st),
            ("Yes", False, "yes"),
            (x1_label, False, "yes" if has_x1 else "unconfirmed"),
            (g.get("publisher") or "", is_z, None),
            (g.get("developer") or "", is_z, None),
            (g.get("year") or "", is_z, None),
            (platforms, is_z, None),
            (g.get("edition") or "Standard", is_z, None),
            (g.get("region") or "Global", is_z, None),
            (g.get("note") or "", is_z, None),
            (g.get("productId") or "", is_z, None),
            (xct_url, is_z, None),
        ]
        for c_idx, (v, z, status_flag) in enumerate(row_vals, 1):
            cell = ws_all_xpa.cell(row=r_idx, column=c_idx)
            style_data_cell(cell, v, is_zebra=z, status=status_flag)
            if c_idx in (7, 9, 10):
                cell.alignment = Alignment(horizontal="center", vertical="center")

    ws_all_xpa.freeze_panes = "A2"
    ws_all_xpa.auto_filter.ref = f"A1:{get_column_letter(len(headers_all_xpa))}{len(xpa_all_sorted)+1}"
    autofit_columns(ws_all_xpa)

    # 5. Sheet: Master D2D Database (All 1,876 Games)
    ws_master = wb.create_sheet(title="Master D2D DB (1876)")
    headers_master = [
        "Game Title", "Disc to Digital Status", "Xbox Play Anywhere (XPA)", "Publisher", "Developer", 
        "Release Year", "Platforms", "Game Pass", "Edition", "Region", "Community Notes", "Product ID"
    ]
    ws_master.row_dimensions[1].height = 28
    for col_idx, h in enumerate(headers_master, 1):
        style_header_cell(ws_master.cell(row=1, column=col_idx), h)

    all_sorted = sorted(games, key=lambda x: x.get("title", "").lower())
    for r_idx, g in enumerate(all_sorted, 2):
        is_z = (r_idx % 2 == 0)
        platforms = ", ".join(g.get("platforms", []))
        game_pass = ", ".join(g.get("gamePass", [])) if g.get("gamePass") else "No"
        st = g.get("status", "").lower()
        st_label = "Yes - Converts" if st == "yes" else "No - Fails" if st == "no" else st.capitalize()
        xpa_flag = "Yes" if g.get("xpa") else "No"

        row_vals = [
            (g.get("title") or "", False, None),
            (st_label, False, st),
            (xpa_flag, False, "yes" if xpa_flag == "Yes" else None),
            (g.get("publisher") or "", is_z, None),
            (g.get("developer") or "", is_z, None),
            (g.get("year") or "", is_z, None),
            (platforms, is_z, None),
            (game_pass, is_z, None),
            (g.get("edition") or "Standard", is_z, None),
            (g.get("region") or "Global", is_z, None),
            (g.get("note") or "", is_z, None),
            (g.get("productId") or "", is_z, None),
        ]
        for c_idx, (v, z, status_flag) in enumerate(row_vals, 1):
            cell = ws_master.cell(row=r_idx, column=c_idx)
            style_data_cell(cell, v, is_zebra=z, status=status_flag)
            if c_idx in (3, 6, 8, 9, 10):
                cell.alignment = Alignment(horizontal="center", vertical="center")

    ws_master.freeze_panes = "A2"
    ws_master.auto_filter.ref = f"A1:{get_column_letter(len(headers_master))}{len(all_sorted)+1}"
    autofit_columns(ws_master)

    # Save
    wb.save(EXCEL_PATH)
    print(f"Workbook successfully saved to: {EXCEL_PATH}")

if __name__ == "__main__":
    games = fetch_all_games()
    build_workbook(games)
