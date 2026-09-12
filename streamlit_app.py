"""The Kivi interface.

    streamlit run streamlit_app.py

Talks to the backend (uvicorn kivi.api.app:app) over HTTP for anything that
writes or touches a model, and reads db/kivi.db directly for the inspector
and the Today view, which are plain SQL.

Everything rendered from the database goes through esc() first -- memory
content is user text, and user text shouldn't be able to break the page.

Visually: a light working canvas, a dark anchored sidebar, and one card
system shared by every section. Section switching is driven by the circular
icon nav at the top of the main area, which writes st.session_state.section.
"""

from __future__ import annotations

import html
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import requests
import streamlit as st

from kivi.formatting import format_fact_row
from kivi.retrieval.tools import get_open_commitments

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "db" / "kivi.db"

st.set_page_config(page_title="Kivi", page_icon="🥝", layout="wide")

# --- Design tokens ---------------------------------------------------------
# Light canvas, white cards, real (not glow) shadows. The kiwi green is
# deepened from the dark theme's #8BE04E so it keeps contrast on white;
# status colours are likewise the saturated variants, not the pastel ones.
CANVAS = "#F4F4F2"
CARD = "#FFFFFF"
TEXT = "#16191D"
MUTED = "#5F6673"
FAINT = "#8A909C"
GREEN = "#3F8F2E"       # fills: active nav circle, primary buttons
GREEN_TEXT = "#2E6F21"  # green as type on light
GREEN_SOFT = "#EAF6E4"
RED = "#C0392B"
RED_TEXT = "#9B1C1C"
RED_SOFT = "#FDECEC"
AMBER_TEXT = "#9A6207"
AMBER_SOFT = "#FDF3E3"
BLUE_TEXT = "#2457C5"

# Plain string, not an f-string: CSS braces stay CSS braces.
st.markdown(
    """
    <style>
      /* ---- chrome ------------------------------------------------------ */
      header[data-testid="stHeader"] { display: none; }
      [data-testid="stToolbar"] { display: none; }
      [data-testid="stDecoration"] { display: none; }
      [data-testid="stSidebarNav"] { display: none; }
      #MainMenu, footer { visibility: hidden; height: 0; }

      [data-testid="stMain"] { background-color: #F4F4F2; }
      .block-container {
        padding-top: 1.8rem; padding-bottom: 4rem; max-width: 1320px;
      }
      /* NB: the :not() is load-bearing. Material Symbols is a ligature font,
         so forcing a sans-serif family onto the icon span stops the ligature
         forming and the raw glyph name ("chat_bubble") renders as text. */
      [data-testid="stMain"], [data-testid="stMain"] p,
      [data-testid="stMain"] span:not([data-testid="stIconMaterial"]),
      [data-testid="stMain"] div, [data-testid="stMain"] label {
        font-family: -apple-system, "Inter", "Geist", BlinkMacSystemFont,
                     "Segoe UI", Roboto, sans-serif;
        color: #16191D;
      }
      /* canonical Material Symbols reset, so ligatures always resolve */
      [data-testid="stIconMaterial"] {
        font-family: "Material Symbols Rounded" !important;
        font-weight: normal !important; font-style: normal !important;
        letter-spacing: normal !important; text-transform: none !important;
        white-space: nowrap !important; word-wrap: normal !important;
        direction: ltr !important;
        -webkit-font-feature-settings: "liga" !important;
        font-feature-settings: "liga" !important;
        -webkit-font-smoothing: antialiased !important;
      }

      /* ---- masthead ---------------------------------------------------- */
      .kv-mast { display: flex; align-items: center; gap: 10px; }
      .kv-mark {
        font-size: 26px; font-weight: 700; letter-spacing: -0.025em;
        color: #16191D; line-height: 1;
      }
      .kv-mark .dot { color: #3F8F2E; }
      [data-testid="stSidebar"] .kv-mark { font-size: 19px; color: #FFFFFF; }
      [data-testid="stSidebar"] .kv-mark .dot { color: #8BE04E; }
      .kv-mast-rule { flex: 1; height: 1px; background: rgba(255, 255, 255, 0.10); }

      /* ---- stat bar ---------------------------------------------------- */
      .kv-stats {
        display: flex; flex-wrap: wrap; gap: 26px;
        align-items: baseline; justify-content: flex-end;
      }
      .kv-stat { display: flex; align-items: baseline; gap: 7px; }
      .kv-stat-v {
        font-size: 22px; font-weight: 700; color: #16191D;
        letter-spacing: -0.03em; font-variant-numeric: tabular-nums; line-height: 1;
      }
      .kv-stat-k {
        font-size: 10.5px; font-weight: 600; color: #8A909C;
        letter-spacing: 0.08em; text-transform: uppercase;
      }
      .kv-stat-d { font-size: 11px; color: #8A909C; }

      /* ---- circular icon nav ------------------------------------------- */
      /* One column per section; the Streamlit <button> element IS the circle.
         Geometry is locked separately from colour, with min/max/flex pinned
         on every interaction state, so no flex or focus recalculation can
         reshape it. The inner icon is clamped and clipped as well: Material
         Symbols is a LIGATURE font, so until it loads the glyph name renders
         as literal text ("chat_bubble") and would otherwise blow the button
         wide -- overflow:hidden on a fixed box absorbs that entirely. */
      .kv-navwrap { margin: 6px 0 2px 0; }
      [class*="st-key-nav_"] .stButton {
        display: flex !important; justify-content: center !important;
      }
      [class*="st-key-nav_"] [data-testid="stElementContainer"] {
        display: flex !important; justify-content: center !important;
      }

      /* -- geometry: identical in every state, nothing here ever varies -- */
      [class*="st-key-nav_"] button,
      [class*="st-key-nav_"] button:hover,
      [class*="st-key-nav_"] button:focus,
      [class*="st-key-nav_"] button:focus-visible,
      [class*="st-key-nav_"] button:active,
      [class*="st-key-nav_"] button:disabled,
      [class*="st-key-nav_"] button[kind="primary"],
      [class*="st-key-nav_"] button[kind="primary"]:hover,
      [class*="st-key-nav_"] button[kind="primary"]:focus,
      [class*="st-key-nav_"] button[kind="primary"]:focus-visible,
      [class*="st-key-nav_"] button[kind="primary"]:active {
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        width: 64px !important;
        height: 64px !important;
        min-width: 64px !important;
        min-height: 64px !important;
        max-width: 64px !important;
        max-height: 64px !important;
        padding: 0 !important;
        margin: 0 !important;
        border-radius: 50% !important;
        flex: 0 0 64px !important;
        flex-shrink: 0 !important;
        flex-grow: 0 !important;
        aspect-ratio: 1 / 1 !important;
        box-sizing: border-box !important;
        overflow: hidden !important;
        white-space: nowrap !important;
        user-select: none !important;
        -webkit-user-select: none !important;
        outline: none !important;
        -webkit-tap-highlight-color: transparent !important;
        transform: none !important;
        transition: background-color 120ms ease, border-color 120ms ease !important;
      }

      /* -- the icon itself: clamped so an unloaded ligature can't push out -- */
      [class*="st-key-nav_"] button > div,
      [class*="st-key-nav_"] button [data-testid="stMarkdownContainer"],
      [class*="st-key-nav_"] button p {
        display: flex !important; align-items: center !important;
        justify-content: center !important;
        margin: 0 !important; padding: 0 !important;
        line-height: 1 !important; overflow: hidden !important;
      }
      [class*="st-key-nav_"] button span,
      [class*="st-key-nav_"] button [data-testid="stIconMaterial"] {
        display: inline-flex !important;
        align-items: center !important; justify-content: center !important;
        font-size: 26px !important; line-height: 1 !important;
        width: 26px !important; height: 26px !important;
        min-width: 26px !important; max-width: 26px !important;
        max-height: 26px !important;
        flex: 0 0 26px !important;
        overflow: hidden !important;
        color: #33383F !important;
      }

      /* -- colour only: no geometry properties below this line -- */
      [class*="st-key-nav_"] button {
        background-color: #E7E7E3;
        border: 1px solid rgba(16, 24, 40, 0.06) !important;
        box-shadow: none !important;
      }
      [class*="st-key-nav_"] button:hover {
        background-color: #DEDEDA;
        border-color: rgba(16, 24, 40, 0.10) !important;
      }
      [class*="st-key-nav_"] button[kind="primary"] {
        background-color: #3F8F2E;
        border-color: #3F8F2E !important;
        box-shadow: 0 4px 14px rgba(63, 143, 46, 0.32) !important;
      }
      [class*="st-key-nav_"] button[kind="primary"]:hover {
        background-color: #37802A;
      }
      /* near-black on this green measures ~5:1, white ~3.7:1 -- pick the darker */
      [class*="st-key-nav_"] button[kind="primary"] span,
      [class*="st-key-nav_"] button[kind="primary"] [data-testid="stIconMaterial"] {
        color: #0D1A08 !important;
      }
      .kv-navlabel {
        text-align: center; font-size: 11.5px; font-weight: 500;
        color: #5F6673; margin-top: 7px; letter-spacing: 0.01em;
        min-height: 2.1em; user-select: none;
      }
      .kv-navlabel.on { color: #2E6F21; font-weight: 650; }

      .kv-rule { height: 1px; background: rgba(16, 24, 40, 0.08); margin: 18px 0 20px 0; }

      /* ---- the card system --------------------------------------------- */
      .kv-card {
        background: #FFFFFF; border-radius: 16px;
        border: 1px solid rgba(16, 24, 40, 0.06);
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05),
                    0 4px 14px rgba(16, 24, 40, 0.06);
        padding: 16px 18px; margin-bottom: 12px;
      }
      .kv-card.dim { opacity: 0.62; }
      .kv-card.warn { border-left: 3px solid #E0A020; }
      .kv-card.bad { border-left: 3px solid #C0392B; }
      /* Streamlit widget groups styled as the same card */
      [data-testid="stForm"],
      .st-key-kv_import, .st-key-kv_reset, .st-key-kv_dictation {
        background: #FFFFFF; border-radius: 16px;
        border: 1px solid rgba(16, 24, 40, 0.06) !important;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05),
                    0 4px 14px rgba(16, 24, 40, 0.06);
        padding: 18px 20px !important;
      }
      .st-key-kv_reset {
        border: 1px solid rgba(192, 57, 43, 0.30) !important;
        box-shadow: 0 1px 2px rgba(192, 57, 43, 0.06),
                    0 4px 14px rgba(192, 57, 43, 0.08);
      }

      .kv-cardhead {
        display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
        font-size: 14.5px; font-weight: 650; color: #16191D; line-height: 1.35;
      }
      .kv-cardhead .sep { color: #C9CDD4; font-weight: 400; }
      .kv-attr { color: #2457C5; font-weight: 600; }
      .kv-num {
        display: inline-flex; align-items: center; justify-content: center;
        min-width: 24px; height: 24px; border-radius: 7px;
        background: #F1F1EE; color: #5F6673;
        font-size: 11.5px; font-weight: 650; font-variant-numeric: tabular-nums;
        padding: 0 6px;
      }
      .kv-body { font-size: 13.5px; color: #3D434D; line-height: 1.55; margin-top: 7px;
                 overflow-wrap: anywhere; }
      .kv-value { font-size: 15px; font-weight: 650; color: #16191D; margin-top: 8px; }
      .kv-note { font-size: 12.5px; color: #5F6673; margin-top: 6px; line-height: 1.5; }
      .kv-note.warn { color: #9A6207; }
      .kv-note.bad { color: #9B1C1C; }
      .kv-edge { color: #2457C5; font-weight: 550; }

      /* ---- metadata row ------------------------------------------------- */
      .kv-meta {
        display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
        margin-top: 11px; padding-top: 10px;
        border-top: 1px solid rgba(16, 24, 40, 0.06);
        font-size: 11.5px; color: #8A909C;
      }
      .kv-meta .m { white-space: nowrap; }
      .kv-meta .m.id {
        font-family: "JetBrains Mono", "Fira Code", "SFMono-Regular", ui-monospace, monospace;
        font-size: 11px; color: #8A909C;
        background: #F4F4F2; border-radius: 5px; padding: 2px 6px;
      }

      /* ---- status pills -------------------------------------------------- */
      .kv-pill {
        display: inline-flex; align-items: center; gap: 6px;
        padding: 3px 9px 3px 7px; border-radius: 999px;
        font-size: 11px; font-weight: 600; letter-spacing: 0.01em;
        background: #F1F1EE; color: #5F6673; white-space: nowrap;
      }
      .kv-pill .d { width: 6px; height: 6px; border-radius: 50%; background: #8A909C; flex: 0 0 6px; }
      .kv-pill.ok   { background: #EAF6E4; color: #2E6F21; } .kv-pill.ok .d   { background: #3F8F2E; }
      .kv-pill.warn { background: #FDF3E3; color: #9A6207; } .kv-pill.warn .d { background: #D9900F; }
      .kv-pill.bad  { background: #FDECEC; color: #9B1C1C; } .kv-pill.bad .d  { background: #C0392B; }
      .kv-pill.info { background: #EAF0FD; color: #2457C5; } .kv-pill.info .d { background: #2457C5; }

      /* dictation progress badges: grey while false, green once true */
      .kv-badges { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
      .kv-badge {
        display: inline-flex; align-items: center; gap: 6px;
        padding: 5px 12px; border-radius: 999px;
        font-size: 11.5px; font-weight: 600;
        background: #ECECE8; color: #6B7280;
        border: 1px solid rgba(16, 24, 40, 0.05);
      }
      .kv-badge .d { width: 6px; height: 6px; border-radius: 50%; background: #A8AEB8; }
      .kv-badge.on { background: #3F8F2E; color: #FFFFFF; border-color: #3F8F2E; }
      .kv-badge.on .d { background: #FFFFFF; }

      .kv-empty {
        background: #FFFFFF; border: 1px dashed rgba(16, 24, 40, 0.14);
        border-radius: 16px; padding: 26px 18px; text-align: center;
        font-size: 13px; color: #8A909C;
      }

      /* ---- section headings ---------------------------------------------- */
      .kv-h {
        display: flex; align-items: center; gap: 10px;
        font-size: 11.5px; font-weight: 700; color: #6B7280;
        letter-spacing: 0.09em; text-transform: uppercase;
        margin: 2px 0 12px 0;
      }
      .kv-h .n {
        font-size: 11px; font-weight: 600; color: #8A909C;
        letter-spacing: 0.02em; text-transform: none;
      }
      .kv-h.danger { color: #9B1C1C; }

      /* ---- chat ------------------------------------------------------------ */
      .kv-turn { display: flex; margin-bottom: 12px; }
      .kv-turn.user { justify-content: flex-end; }
      .kv-bubble {
        max-width: 78%; border-radius: 16px; padding: 13px 16px;
        font-size: 14px; line-height: 1.55;
      }
      .kv-bubble.assistant {
        background: #FFFFFF; color: #16191D; border-bottom-left-radius: 5px;
        border: 1px solid rgba(16, 24, 40, 0.06);
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05), 0 4px 14px rgba(16, 24, 40, 0.06);
      }
      .kv-bubble.user {
        background: #E7F4E0; color: #1A2E13; border-bottom-right-radius: 5px;
        border: 1px solid rgba(63, 143, 46, 0.18);
      }
      .kv-bubble .kv-pill { margin-top: 9px; }
      .kv-srcwrap { max-width: 78%; margin: -4px 0 14px 0; }

      /* ---- segmented control (Memory sub-sections) ------------------------- */
      .st-key-kv_memseg {
        background: #E9E9E5; border-radius: 999px; padding: 4px;
        margin-bottom: 16px; border: 1px solid rgba(16, 24, 40, 0.05);
      }
      .st-key-kv_memseg [data-testid="stHorizontalBlock"] { gap: 3px; }
      [class*="st-key-seg_"] button {
        width: 100%; border-radius: 999px; background: transparent;
        border: none !important; box-shadow: none; color: #5F6673;
        font-size: 12.5px; font-weight: 550; padding: 6px 4px; min-height: 0;
      }
      [class*="st-key-seg_"] button:hover { background: rgba(255, 255, 255, 0.55); color: #16191D; }
      [class*="st-key-seg_"] button[kind="primary"] {
        background: #FFFFFF; color: #2E6F21; font-weight: 700;
        box-shadow: 0 1px 3px rgba(16, 24, 40, 0.14);
      }
      [class*="st-key-seg_"] button[kind="primary"]:hover { background: #FFFFFF; }

      /* ---- main-area widgets on light -------------------------------------- */
      [data-testid="stMain"] .stTextInput input,
      [data-testid="stMain"] .stTextArea textarea,
      [data-testid="stMain"] .stSelectbox div[data-baseweb="select"] > div {
        background-color: #FFFFFF !important;
        border: 1px solid rgba(16, 24, 40, 0.12) !important;
        border-radius: 10px !important; color: #16191D !important;
        font-size: 13.5px !important; box-shadow: none !important;
      }
      [data-testid="stMain"] .stTextArea textarea { line-height: 1.6 !important; padding: 12px !important; }
      [data-testid="stMain"] .stTextInput input:focus,
      [data-testid="stMain"] .stTextArea textarea:focus {
        border-color: #3F8F2E !important;
        box-shadow: 0 0 0 3px rgba(63, 143, 46, 0.14) !important;
      }
      [data-testid="stMain"] .stTextInput label, [data-testid="stMain"] .stTextArea label,
      [data-testid="stMain"] .stSelectbox label, [data-testid="stMain"] .stFileUploader label {
        font-size: 10.5px !important; color: #6B7280 !important;
        text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700 !important;
      }
      [data-testid="stMain"] .stButton button,
      [data-testid="stMain"] .stFormSubmitButton button {
        background-color: #FFFFFF; color: #16191D;
        border: 1px solid rgba(16, 24, 40, 0.14); border-radius: 10px;
        font-size: 13px; font-weight: 600; padding: 7px 16px;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
      }
      [data-testid="stMain"] .stButton button:hover,
      [data-testid="stMain"] .stFormSubmitButton button:hover {
        background-color: #FAFAF9; border-color: rgba(16, 24, 40, 0.22); color: #16191D;
      }
      [data-testid="stMain"] .stFormSubmitButton button[kind="primaryFormSubmit"],
      [data-testid="stMain"] .stButton button[kind="primary"] {
        background-color: #3F8F2E; border-color: #3F8F2E; color: #FFFFFF;
        box-shadow: 0 2px 8px rgba(63, 143, 46, 0.26);
      }
      [data-testid="stMain"] .stFormSubmitButton button[kind="primaryFormSubmit"]:hover,
      [data-testid="stMain"] .stButton button[kind="primary"]:hover {
        background-color: #37802A; color: #FFFFFF;
      }
      /* the destructive action keeps its own red treatment */
      .st-key-kv_resetbtn button {
        background-color: #C0392B !important; border-color: #C0392B !important;
        color: #FFFFFF !important; box-shadow: 0 2px 8px rgba(192, 57, 43, 0.26) !important;
      }
      .st-key-kv_resetbtn button:hover { background-color: #A93226 !important; }
      .st-key-kv_resetbtn button:disabled {
        background-color: #E8C4BF !important; border-color: #E8C4BF !important;
        color: #FFFFFF !important; box-shadow: none !important;
      }
      [data-testid="stMain"] .stCheckbox label p { font-size: 13px !important; color: #3D434D !important; }
      [data-testid="stMain"] [data-testid="stFileUploaderDropzone"] {
        background-color: #FAFAF9; border: 1px dashed rgba(16, 24, 40, 0.16);
        border-radius: 12px;
      }
      [data-testid="stMain"] [data-testid="stExpander"] details {
        border: 1px solid rgba(16, 24, 40, 0.08); border-radius: 12px;
        background: #FFFFFF; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
      }
      [data-testid="stMain"] [data-testid="stExpander"] summary {
        font-size: 12.5px; color: #5F6673; font-weight: 600;
      }
      [data-testid="stMain"] [data-testid="stCodeBlock"] pre {
        background-color: #F7F7F5 !important; border: 1px solid rgba(16, 24, 40, 0.07);
        border-radius: 10px; color: #16191D !important;
      }
      [data-testid="stMain"] code {
        font-family: "JetBrains Mono", "Fira Code", ui-monospace, monospace !important;
        font-size: 12px !important;
      }
      /* chat input as a rounded pill */
      [data-testid="stChatInput"] {
        border-radius: 999px !important; background: #FFFFFF !important;
        border: 1px solid rgba(16, 24, 40, 0.12) !important;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05), 0 6px 18px rgba(16, 24, 40, 0.07);
        padding: 2px 6px !important;
      }
      [data-testid="stChatInput"] textarea { font-size: 14px !important; color: #16191D !important; }
      [data-testid="stChatInput"] button {
        border-radius: 50% !important; background: #3F8F2E !important; color: #FFFFFF !important;
      }
      [data-testid="stBottomBlockContainer"] { background: #F4F4F2; }

      /* ---- alerts ---------------------------------------------------------- */
      [data-testid="stAlert"] {
        border-radius: 12px; font-size: 13px;
        border: 1px solid rgba(16, 24, 40, 0.08);
      }
      /* the backend-unreachable banner, light-theme treatment even in the
         dark sidebar, so it reads as an alert rather than sinking in */
      [data-testid="stSidebar"] [data-testid="stAlert"],
      [data-testid="stMain"] [data-testid="stAlert"] {
        background-color: #FDECEC; border: 1px solid rgba(192, 57, 43, 0.28);
      }
      [data-testid="stSidebar"] [data-testid="stAlert"] *,
      [data-testid="stMain"] [data-testid="stAlert"] * { color: #9B1C1C !important; }
      [data-testid="stSidebar"] [data-testid="stAlert"] code,
      [data-testid="stMain"] [data-testid="stAlert"] code {
        background: rgba(192, 57, 43, 0.10); color: #7F1717 !important; border-radius: 5px;
      }

      /* ---- sidebar (dark, structure unchanged) ------------------------------ */
      [data-testid="stSidebar"] { border-right: 1px solid rgba(255, 255, 255, 0.07); }
      [data-testid="stSidebar"] .block-container { padding-top: 1.5rem; }
      [data-testid="stSidebar"] .kv-h { color: #8A909C; }
      [data-testid="stSidebar"] .stTextInput input {
        background-color: #202329 !important; color: #E6E8EA !important;
        border: 1px solid rgba(255, 255, 255, 0.10) !important; border-radius: 10px !important;
        font-size: 13px !important;
      }
      [data-testid="stSidebar"] .stTextInput input:focus { border-color: #8BE04E !important; }
      [data-testid="stSidebar"] .stTextInput label {
        font-size: 10.5px !important; color: #8A909C !important;
        text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700 !important;
      }
      [data-testid="stSidebar"] .stButton button {
        background-color: #24272D; color: #E6E8EA;
        border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 10px;
        font-size: 12.5px; font-weight: 600; box-shadow: none;
      }
      [data-testid="stSidebar"] .stButton button:hover {
        background-color: #2C3037; border-color: rgba(255, 255, 255, 0.22); color: #FFFFFF;
      }
      [data-testid="stSidebar"] [data-testid="stCodeBlock"] pre {
        background-color: #202329 !important; border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 10px; color: #E6E8EA !important; font-size: 11.5px !important;
      }
      [data-testid="stSidebar"] hr { border-color: rgba(255, 255, 255, 0.09); margin: 16px 0; }
      [data-testid="stSidebar"] .kv-pill { background: rgba(255, 255, 255, 0.07); color: #B9BFC8; }
      [data-testid="stSidebar"] .kv-pill.ok { background: rgba(139, 224, 78, 0.14); color: #A9E87B; }
      [data-testid="stSidebar"] .kv-pill.ok .d { background: #8BE04E; }
      [data-testid="stSidebar"] .kv-pill.bad { background: rgba(240, 100, 90, 0.16); color: #F0968E; }
      [data-testid="stSidebar"] .kv-pill.bad .d { background: #E5544A; }
      [data-testid="stSidebar"] .kv-store {
        display: flex; flex-wrap: wrap; gap: 8px;
      }
      [data-testid="stSidebar"] .kv-store span {
        background: rgba(255, 255, 255, 0.06); border-radius: 7px;
        padding: 5px 9px; font-size: 11.5px; color: #B9BFC8; white-space: nowrap;
      }
      [data-testid="stSidebar"] .kv-store b { color: #FFFFFF; font-weight: 650; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def esc(value) -> str:
    """Escape anything coming out of the database before it reaches a card."""
    return html.escape("" if value is None else str(value))


def pill(label: str, tone: str = "", dot: bool = True) -> str:
    """Status pill: a coloured dot and a label."""
    marker = '<span class="d"></span>' if dot else ""
    return f'<span class="kv-pill {tone}">{marker}{esc(label)}</span>'


def badge(label: str, on: bool = False) -> str:
    """Dictation progress badge: grey while false, green once true."""
    state = "on" if on else ""
    return f'<span class="kv-badge {state}"><span class="d"></span>{esc(label)}</span>'


def meta(*items: str) -> str:
    """The card's bottom metadata row. Items must already be escaped."""
    cells = "".join(f'<span class="m">{i}</span>' for i in items if i)
    return f'<div class="kv-meta">{cells}</div>' if cells else ""


def mono_id(value) -> str:
    return f'<span class="m id">{esc(value)}</span>' if value else ""


def card(head: str, body: str = "", meta_html: str = "", state: str = "") -> str:
    """One discrete unit of content, in the shared card treatment."""
    parts = [f'<div class="kv-card {state}">']
    if head:
        parts.append(f'<div class="kv-cardhead">{head}</div>')
    if body:
        parts.append(body)
    if meta_html:
        parts.append(meta_html)
    parts.append("</div>")
    return "".join(parts)


def cards(items: list, empty: str = "Nothing here.") -> str:
    return "".join(items) if items else f'<div class="kv-empty">{esc(empty)}</div>'


def heading(label: str, note: str = "", danger: bool = False) -> str:
    suffix = f'<span class="n">{esc(note)}</span>' if note else ""
    cls = "kv-h danger" if danger else "kv-h"
    return f'<div class="{cls}">{esc(label)}{suffix}</div>'


def stats(items: list) -> str:
    """[(value, label, delta)] as the top-right stat bar."""
    cells = []
    for value, label, delta in items:
        extra = f'<span class="kv-stat-d">{esc(delta)}</span>' if delta else ""
        cells.append(
            f'<span class="kv-stat"><span class="kv-stat-v">{esc(value)}</span>'
            f'<span class="kv-stat-k">{esc(label)}</span>{extra}</span>'
        )
    return f'<div class="kv-stats">{"".join(cells)}</div>'


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

if "session_id" not in st.session_state:
    st.session_state.session_id = f"sess_{uuid.uuid4().hex[:12]}"
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []  # list of {role, content, citations?, metrics?}
if "base_url" not in st.session_state:
    st.session_state.base_url = "http://localhost:8000"
if "section" not in st.session_state:
    st.session_state.section = "hey"
if "mem_seg" not in st.session_state:
    st.session_state.mem_seg = "facts"


def _set_section(key: str) -> None:
    """Nav click handler. Runs before the rerun, so one pass does the switch."""
    st.session_state.section = key


def _set_mem_seg(key: str) -> None:
    st.session_state.mem_seg = key


def api(method: str, path: str, **kwargs):
    url = st.session_state.base_url.rstrip("/") + path
    try:
        resp = requests.request(method, url, timeout=120, **kwargs)
    except requests.exceptions.ConnectionError:
        st.error(
            f"Can't reach the Kivi backend at {st.session_state.base_url}. "
            f"Start it with: uvicorn kivi.api.app:app --reload"
        )
        return None
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        st.error(f"{resp.status_code}: {detail}")
        return None
    return resp.json()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _state_badge(is_active, deleted_at) -> tuple[str, str]:
    """(pill html, card state)."""
    if deleted_at:
        return pill("deleted", "bad") + ("" if is_active else pill("superseded")), "dim"
    if not is_active:
        return pill("superseded"), "dim"
    return pill("current", "ok"), ""


def memory_counts() -> dict:
    if not DB_PATH.exists():
        return {}
    conn = get_db()
    try:
        q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "facts": q("SELECT COUNT(*) FROM declarative_facts WHERE is_active=1 AND deleted_at IS NULL"),
            "events": q("SELECT COUNT(*) FROM episodic_events WHERE deleted_at IS NULL"),
            "commitments": q("SELECT COUNT(*) FROM commitments WHERE deleted_at IS NULL"),
            "preferences": q("SELECT COUNT(*) FROM preferences WHERE is_active=1 AND deleted_at IS NULL"),
            "entities": q("SELECT COUNT(*) FROM entities"),
            "captures": q("SELECT COUNT(*) FROM captures"),
            "relationships": q("SELECT COUNT(*) FROM relationships"),
            "ignored": q("SELECT COUNT(*) FROM captures WHERE extraction_status != 'processed'"),
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


counts = memory_counts()

# ---------------------------------------------------------------------------
# Sidebar -- structure unchanged, restyled to match the new tokens
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(
        '<div class="kv-mast"><span class="kv-mark">kiv<span class="dot">i</span></span>'
        '<span class="kv-mast-rule"></span></div>',
        unsafe_allow_html=True,
    )
    st.divider()

    st.session_state.base_url = st.text_input("Backend URL", value=st.session_state.base_url)
    health = api("GET", "/health")
    st.markdown(
        pill("backend online", "ok") if health else pill("backend unreachable", "bad"),
        unsafe_allow_html=True,
    )

    st.divider()
    st.markdown(heading("Session"), unsafe_allow_html=True)
    st.code(st.session_state.session_id, language=None)
    if st.button("New session", use_container_width=True):
        st.session_state.session_id = f"sess_{uuid.uuid4().hex[:12]}"
        st.session_state.chat_history = []
        st.rerun()

    if counts:
        st.divider()
        st.markdown(heading("Store"), unsafe_allow_html=True)
        st.markdown(
            '<div class="kv-store">'
            f'<span><b>{counts["entities"]}</b> entities</span>'
            f'<span><b>{counts["captures"]}</b> captures</span>'
            f'<span><b>{counts["relationships"]}</b> relationships</span>'
            f'<span><b>{counts["ignored"]}</b> ignored</span>'
            "</div>",
            unsafe_allow_html=True,
        )

# ---------------------------------------------------------------------------
# Masthead
# ---------------------------------------------------------------------------

head_left, head_right = st.columns([1, 3])
with head_left:
    st.markdown(
        '<div class="kv-mast"><span class="kv-mark">kiv<span class="dot">i</span></span></div>',
        unsafe_allow_html=True,
    )
with head_right:
    if counts:
        st.markdown(
            stats(
                [
                    (str(counts["facts"]), "facts", ""),
                    (str(counts["events"]), "events", ""),
                    (str(counts["commitments"]), "commitments", ""),
                    (str(counts["preferences"]), "preferences", ""),
                ]
            ),
            unsafe_allow_html=True,
        )

# ---------------------------------------------------------------------------
# Circular icon nav -- replaces the tab strip, same position, same 6 sections
# ---------------------------------------------------------------------------

SECTIONS = [
    ("hey", "Hey Kivi", "chat_bubble"),
    ("today", "Today", "checklist"),
    ("put", "Put info", "edit_note"),
    ("dict", "Dictation", "mic"),
    ("mem", "Memory", "database"),
    ("admin", "Corpus & reset", "restart_alt"),
]

st.markdown('<div class="kv-navwrap"></div>', unsafe_allow_html=True)
nav_cols = st.columns(len(SECTIONS))
for col, (key, label, icon) in zip(nav_cols, SECTIONS):
    with col:
        is_on = st.session_state.section == key
        # on_click runs before the rerun the click already triggers, so the
        # new section is live on the next pass -- no second st.rerun() needed.
        st.button(
            "",
            key=f"nav_{key}",
            icon=f":material/{icon}:",
            type="primary" if is_on else "secondary",
            help=label,
            on_click=_set_section,
            args=(key,),
        )
        st.markdown(
            f'<div class="kv-navlabel {"on" if is_on else ""}">{esc(label)}</div>',
            unsafe_allow_html=True,
        )

st.markdown('<div class="kv-rule"></div>', unsafe_allow_html=True)
section = st.session_state.section

# ---------------------------------------------------------------------------
# Hey Kivi -- conversational agent
# ---------------------------------------------------------------------------

RESPONSE_PILL = {
    "answer": ("answered", "ok"),
    "abstain": ("no record", "warn"),
    "needs_disambiguation": ("needs a choice", "warn"),
    "needs_clarification": ("needs detail", "warn"),
}


def render_citations(citations: list) -> None:
    """Provenance: what was said, in which app, when."""
    if not citations:
        return
    label = f"{len(citations)} source" + ("s" if len(citations) != 1 else "")
    st.markdown('<div class="kv-srcwrap">', unsafe_allow_html=True)
    with st.expander(label):
        st.markdown(
            cards(
                [
                    card(
                        head=pill(c.get("source_type", "memory"), "info"),
                        body='<div class="kv-body">“'
                        + esc(c.get("formatted_text") or c.get("snippet") or "")
                        + "”</div>",
                        meta_html=meta(
                            esc(c.get("foreground_app") or "manual entry"),
                            esc((c.get("captured_at") or "")[:16].replace("T", " ")),
                            mono_id(c.get("source_id", "")),
                        ),
                    )
                    for c in citations
                ]
            ),
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)


def chat_bubble(role: str, content: str, extra: str = "") -> str:
    return (
        f'<div class="kv-turn {role}"><div class="kv-bubble {role}">'
        f"{esc(content).replace(chr(10), '<br>')}{extra}</div></div>"
    )


if section == "hey":
    for turn in st.session_state.chat_history:
        if turn["role"] == "user":
            st.markdown(chat_bubble("user", turn["content"]), unsafe_allow_html=True)
            continue

        extra = ""
        if turn.get("kind"):
            label, tone = RESPONSE_PILL.get(turn["kind"], (turn["kind"], ""))
            extra += f'<div class="kv-badges">{pill(label, tone)}</div>'
        if turn.get("metrics"):
            m = turn["metrics"]
            extra += (
                '<div class="kv-meta">'
                f'<span class="m">{m["total_latency_ms"]:.0f}ms total</span>'
                f'<span class="m">{m["retrieval_latency_ms"]:.0f}ms retrieval</span>'
                f'<span class="m">{m["tool_calls"]} tool calls</span>'
                f'<span class="m">{m["model_calls"]} model calls</span>'
                "</div>"
            )
        st.markdown(chat_bubble("assistant", turn["content"], extra), unsafe_allow_html=True)
        render_citations(turn.get("citations") or [])

    question = st.chat_input("Hey Kivi…")
    if question:
        st.session_state.chat_history.append({"role": "user", "content": question})
        st.markdown(chat_bubble("user", question), unsafe_allow_html=True)
        with st.spinner("Searching memory…"):
            result = api(
                "POST", "/query",
                # headless_mode=False, or clarification and disambiguation never reach
                # the interface at all
                json={
                    "question": question,
                    "session_id": st.session_state.session_id,
                    "headless_mode": False,
                },
            )
        if result:
            st.session_state.session_id = result["session_id"]
            kind = result["response_type"]
            text = (
                result.get("response_text")
                or result.get("clarification_prompt")
                or result.get("abstain_reason")
                or "(no answer)"
            )
            if kind == "abstain":
                text = f"I don't have a record of that. {result.get('abstain_reason', '')}"
            elif kind == "needs_disambiguation":
                options = result.get("disambiguation_options") or []
                text = "Which did you mean?\n\n" + "\n".join(f"- {o}" for o in options)
            st.session_state.chat_history.append(
                {
                    "role": "assistant",
                    "content": text,
                    "kind": kind,
                    "citations": result.get("citations"),
                    "metrics": result.get("metrics"),
                }
            )
            st.rerun()

# ---------------------------------------------------------------------------
# Today -- outstanding work, ordered
# ---------------------------------------------------------------------------

elif section == "today":
    if not DB_PATH.exists():
        st.warning(f"No database found at {DB_PATH}. Import a corpus first.")
    else:
        conn = get_db()
        try:
            entities = conn.execute(
                "SELECT entity_id, canonical_name FROM entities ORDER BY canonical_name"
            ).fetchall()
            names = {"Everything": None} | {e["canonical_name"]: e["entity_id"] for e in entities}
            picked = st.selectbox("Scope", list(names), index=0)
            plan = get_open_commitments(conn, entity_id=names[picked], limit=15)

            if plan.get("warnings"):
                for w in plan["warnings"]:
                    st.warning(w)

            counts_row = plan.get("counts") or {}
            left, right = st.columns([3, 2])

            with left:
                st.markdown(
                    heading("Do next", f'{counts_row.get("actionable", 0)} actionable'),
                    unsafe_allow_html=True,
                )
                items = []
                for item in plan["ordered"]:
                    overdue = "overdue" in (item.get("why_here") or "")
                    status_pill = pill(
                        item["status"].replace("_", " "),
                        "ok" if item["status"] == "in_progress" else "",
                    )
                    body = ""
                    if item.get("description"):
                        body += f'<div class="kv-body">{esc(item["description"])}</div>'
                    if item.get("why_here"):
                        body += (
                            f'<div class="kv-note {"warn" if overdue else ""}">'
                            f'{esc(item["why_here"])}</div>'
                        )
                    if item.get("must_follow"):
                        body += (
                            '<div class="kv-note">must follow <span class="kv-edge">'
                            + esc(", ".join(item["must_follow"]))
                            + "</span></div>"
                        )
                    elif item.get("unblocks"):
                        body += (
                            '<div class="kv-note">unblocks <span class="kv-edge">'
                            + esc(", ".join(item["unblocks"]))
                            + "</span></div>"
                        )
                    items.append(
                        card(
                            head=f'<span class="kv-num">{item["rank"]:02d}</span>'
                            f'<span>{esc(item["commitment"])}</span>{status_pill}',
                            body=body,
                            meta_html=meta(
                                esc(item.get("entity") or "no project"),
                                mono_id(item["commitment_id"]),
                            ),
                            state="warn" if overdue else "",
                        )
                    )
                st.markdown(cards(items, "Nothing outstanding."), unsafe_allow_html=True)

            with right:
                st.markdown(
                    heading("Waiting on", f'{counts_row.get("blocked", 0)} blocked'),
                    unsafe_allow_html=True,
                )
                items = [
                    card(
                        head=f'<span>{esc(item["commitment"])}</span>{pill("blocked", "bad")}',
                        body='<div class="kv-note bad">'
                        + esc(item.get("blocking_reason") or "no reason recorded")
                        + "</div>",
                        meta_html=meta(esc(item.get("entity") or "no project")),
                        state="bad",
                    )
                    for item in plan["blocked"]
                ]
                st.markdown(cards(items, "Nothing is blocked."), unsafe_allow_html=True)
        finally:
            conn.close()

# ---------------------------------------------------------------------------
# Put Info -- manual, session-aware ingestion
# ---------------------------------------------------------------------------

elif section == "put":
    with st.form("put_info_form", clear_on_submit=True):
        content = st.text_area(
            "Update or note", height=140,
            placeholder="The Helix cutover moved to the 14th and Priya is covering it.",
        )
        foreground_app = st.text_input("Source app (optional)")
        submitted = st.form_submit_button("Save to memory", type="primary")

    if submitted and content.strip():
        metadata = {"foreground_app": foreground_app} if foreground_app.strip() else None
        with st.spinner("Extracting…"):
            result = api(
                "POST", "/ingest",
                json={
                    "content": content,
                    "session_id": st.session_state.session_id,
                    "metadata": metadata,
                    "source_type": "selected_text",
                },
            )
        if result:
            if result.get("condensed_content"):
                st.info(f"Resolved to: “{result['condensed_content']}”")
            status = result["extraction_status"]
            if status == "processed":
                st.markdown(
                    card(
                        head=pill("saved", "ok") + "<span>Written to memory</span>",
                        body=stats(
                            [
                                (
                                    f'+{result["facts_inserted"]}',
                                    "facts",
                                    f'{result["facts_superseded"]} superseded'
                                    if result["facts_superseded"]
                                    else "",
                                ),
                                (f'+{result["events_inserted"]}', "events", ""),
                                (f'+{result["commitments_created"]}', "commitments", ""),
                                (
                                    f'+{result["preferences_inserted"]}',
                                    "preferences",
                                    f'{result["preferences_superseded"]} superseded'
                                    if result["preferences_superseded"]
                                    else "",
                                ),
                            ]
                        ).replace('class="kv-stats"', 'class="kv-stats" style="justify-content:flex-start"'),
                    ),
                    unsafe_allow_html=True,
                )
                for w in result["warnings"]:
                    st.warning(w)
            else:
                st.warning(f"Not saved — {status}: {result.get('discard_reason', '')}")
            with st.expander("Raw response"):
                st.json(result)

# ---------------------------------------------------------------------------
# Dictation -- no memory, on purpose
# ---------------------------------------------------------------------------

elif section == "dict":
    with st.container(key="kv_dictation"):
        st.markdown(
            f'<div class="kv-h">Dictation{pill("memory off", "bad")}</div>',
            unsafe_allow_html=True,
        )
        dictation_text = st.text_area(
            "Dictate", height=240, key="dictation_box", label_visibility="collapsed",
            placeholder="Speak and Kivi writes.",
        )
        words = len(dictation_text.split())
        st.markdown(
            '<div class="kv-badges">'
            + badge(f"{words} words", words > 0)
            + badge("not stored")
            + badge("not extracted")
            + badge("not searchable")
            + "</div>",
            unsafe_allow_html=True,
        )

# ---------------------------------------------------------------------------
# Memory inspector -- direct, read-only SQLite view
# ---------------------------------------------------------------------------

elif section == "mem":
    if not DB_PATH.exists():
        st.warning(f"No database found at {DB_PATH}. Run a reset or an ingestion first.")
    else:
        MEM_SEGMENTS = [
            ("facts", "Facts"),
            ("events", "Events"),
            ("commitments", "Commitments"),
            ("preferences", "Preferences"),
            ("connections", "Connections"),
            ("log", "Decision log"),
            ("captures", "Captures"),
        ]
        with st.container(key="kv_memseg"):
            seg_cols = st.columns(len(MEM_SEGMENTS))
            for col, (seg_key, seg_label) in zip(seg_cols, MEM_SEGMENTS):
                with col:
                    st.button(
                        seg_label,
                        key=f"seg_{seg_key}",
                        type="primary" if st.session_state.mem_seg == seg_key else "secondary",
                        use_container_width=True,
                        on_click=_set_mem_seg,
                        args=(seg_key,),
                    )
        seg = st.session_state.mem_seg

        conn = get_db()
        try:
            entities = conn.execute("SELECT * FROM entities ORDER BY created_at DESC").fetchall()
            entity_names = {e["entity_id"]: e["canonical_name"] for e in entities}

            if seg == "facts":
                show_inactive = st.checkbox(
                    "Show superseded and deleted", value=False, key="facts_show_inactive"
                )
                db_rows = conn.execute(
                    "SELECT * FROM declarative_facts "
                    + ("" if show_inactive else "WHERE is_active = 1 AND deleted_at IS NULL ")
                    + "ORDER BY created_at DESC, rowid DESC LIMIT 200"
                ).fetchall()
                items = []
                for r in db_rows:
                    # value_text and value_numeric are exclusive; format_fact_row joins them
                    state_pill, state = _state_badge(r["is_active"], r["deleted_at"])
                    items.append(
                        card(
                            head=f'<span>{esc(entity_names.get(r["entity_id"], r["entity_id"]))}</span>'
                            f'<span class="sep">/</span>'
                            f'<span class="kv-attr">{esc(r["attribute"])}</span>{state_pill}',
                            body=f'<div class="kv-value">{esc(format_fact_row(r))}</div>',
                            meta_html=meta(mono_id(r["fact_id"]), mono_id(r["source_capture_id"])),
                            state=state,
                        )
                    )
                st.markdown(
                    cards(items, "No facts yet." if show_inactive else "No active facts yet."),
                    unsafe_allow_html=True,
                )

            elif seg == "events":
                show_deleted_events = st.checkbox(
                    "Show deleted", value=False, key="events_show_deleted"
                )
                db_rows = conn.execute(
                    "SELECT * FROM episodic_events "
                    + ("" if show_deleted_events else "WHERE deleted_at IS NULL ")
                    + "ORDER BY COALESCE(resolved_time, created_at) DESC, rowid DESC LIMIT 200"
                ).fetchall()
                items = []
                for r in db_rows:
                    when = r["resolved_time"] or r["relative_time_expression"] or "unresolved"
                    is_fix = r["event_type"] == "resolution_found"
                    items.append(
                        card(
                            head=pill(r["event_type"].replace("_", " "), "ok" if is_fix else "")
                            + (pill("deleted", "bad") if r["deleted_at"] else ""),
                            body=f'<div class="kv-body">{esc(r["description"])}</div>',
                            meta_html=meta(
                                esc(entity_names.get(r["entity_id"], "no project")),
                                esc(when),
                            ),
                            state="dim" if r["deleted_at"] else "",
                        )
                    )
                st.markdown(cards(items, "No events yet."), unsafe_allow_html=True)

            elif seg == "commitments":
                show_closed = st.checkbox(
                    "Show done and deleted", value=False, key="commitments_show_closed"
                )
                db_rows = conn.execute(
                    "SELECT co.*, cse.status, cse.blocking_reason, cse.due_date_resolved "
                    "FROM commitments co "
                    "LEFT JOIN commitment_status_events cse "
                    "  ON cse.commitment_id = co.commitment_id AND cse.is_active = 1 "
                    + (
                        ""
                        if show_closed
                        else "WHERE co.deleted_at IS NULL AND COALESCE(cse.status, 'open') != 'done' "
                    )
                    + "ORDER BY co.created_at DESC, co.rowid DESC LIMIT 200"
                ).fetchall()
                items = []
                for r in db_rows:
                    status = r["status"] or "no status"
                    tone = {"done": "ok", "blocked": "bad", "in_progress": "ok"}.get(status, "")
                    details = []
                    if r["blocking_reason"]:
                        details.append(f'waiting on {esc(r["blocking_reason"])}')
                    if r["due_date_resolved"]:
                        details.append(f'due {esc(r["due_date_resolved"][:10])}')
                    body = f'<div class="kv-body">{esc(r["description"])}</div>'
                    if details:
                        body += f'<div class="kv-note">{" · ".join(details)}</div>'
                    items.append(
                        card(
                            head=f'<span>{esc(r["commitment_mention"])}</span>'
                            + pill(status.replace("_", " "), tone)
                            + (pill("deleted", "bad") if r["deleted_at"] else ""),
                            body=body,
                            meta_html=meta(
                                esc(entity_names.get(r["entity_id"], "no project")),
                                mono_id(r["commitment_id"]),
                            ),
                            state="dim" if r["deleted_at"] else "",
                        )
                    )
                st.markdown(
                    cards(items, "No commitments yet." if show_closed else "No open commitments."),
                    unsafe_allow_html=True,
                )

            elif seg == "preferences":
                show_inactive_prefs = st.checkbox(
                    "Show superseded and deleted", value=False, key="prefs_show_inactive"
                )
                db_rows = conn.execute(
                    "SELECT * FROM preferences "
                    + ("" if show_inactive_prefs else "WHERE is_active = 1 AND deleted_at IS NULL ")
                    + "ORDER BY created_at DESC, rowid DESC LIMIT 200"
                ).fetchall()
                items = []
                for r in db_rows:
                    state_pill, state = _state_badge(r["is_active"], r["deleted_at"])
                    scope = entity_names.get(r["entity_id"], "general")
                    items.append(
                        card(
                            head=pill(r["category"] or "uncategorised", "", dot=False)
                            + f'<span class="sep">/</span><span>{esc(scope)}</span>{state_pill}',
                            body=f'<div class="kv-body">{esc(r["preference_text"])}</div>',
                            meta_html=meta(
                                mono_id(r["preference_id"]), mono_id(r["source_capture_id"])
                            ),
                            state=state,
                        )
                    )
                st.markdown(cards(items, "No preferences yet."), unsafe_allow_html=True)

            elif seg == "connections":
                db_rows = conn.execute(
                    "SELECT rel.*, "
                    "  COALESCE((SELECT description FROM episodic_events WHERE event_id=rel.source_id), "
                    "           (SELECT commitment_mention FROM commitments WHERE commitment_id=rel.source_id)) src, "
                    "  COALESCE((SELECT description FROM episodic_events WHERE event_id=rel.target_id), "
                    "           (SELECT commitment_mention FROM commitments WHERE commitment_id=rel.target_id)) tgt "
                    "FROM relationships rel ORDER BY rel.created_at DESC, rel.rowid DESC LIMIT 200"
                ).fetchall()
                items = []
                for r in db_rows:
                    verb = {"resolves": "was fixed by", "must_precede": "must happen before"}.get(
                        r["relationship_type"], r["relationship_type"].replace("_", " ")
                    )
                    items.append(
                        card(
                            head=pill(r["relationship_type"], "info"),
                            body=f'<div class="kv-body">{esc(r["src"] or r["source_id"])}</div>'
                            f'<div class="kv-note"><span class="kv-edge">↓ {esc(verb)}</span></div>'
                            f'<div class="kv-body">{esc(r["tgt"] or r["target_id"])}</div>',
                            meta_html=meta(mono_id(r["source_capture_id"])),
                        )
                    )
                st.markdown(cards(items, "No connections yet."), unsafe_allow_html=True)

            elif seg == "log":
                db_rows = conn.execute(
                    "SELECT * FROM decision_logs ORDER BY created_at DESC, rowid DESC LIMIT 200"
                ).fetchall()
                items = []
                for r in db_rows:
                    memorized = r["decision"] == "memorized"
                    if r["reason"]:
                        body = esc(r["reason"])
                    else:
                        body = (
                            f'facts +{r["facts_created"]} · events +{r["events_created"]} · '
                            f'commitments +{r["commitments_created"]} · preferences +{r["preferences_created"]}'
                        )
                        if r["latency_ms"] is not None:
                            body += f' · {r["latency_ms"]:.0f}ms'
                    items.append(
                        card(
                            head=pill(
                                "remembered" if memorized else "ignored",
                                "ok" if memorized else "warn",
                            ),
                            body=f'<div class="kv-note">{body}</div>',
                            meta_html=meta(mono_id(r["capture_id"])),
                            state="" if memorized else "dim",
                        )
                    )
                st.markdown(cards(items, "No decisions logged yet."), unsafe_allow_html=True)

            elif seg == "captures":
                db_rows = conn.execute(
                    "SELECT * FROM captures ORDER BY ingested_at DESC, rowid DESC LIMIT 200"
                ).fetchall()
                items = []
                for r in db_rows:
                    ok = r["extraction_status"] == "processed"
                    items.append(
                        card(
                            head=pill(r["extraction_status"].replace("_", " "), "ok" if ok else "warn")
                            + pill(r["source_modality"].replace("_", " "), "", dot=False),
                            body=f'<div class="kv-body">'
                            f'{esc(r["formatted_text"] or r["raw_asr_text"] or "")}</div>',
                            meta_html=meta(
                                esc(r["foreground_app"] or "—"),
                                esc((r["captured_at"] or "")[:16].replace("T", " ")),
                                mono_id(r["capture_id"]),
                            ),
                            state="" if ok else "dim",
                        )
                    )
                st.markdown(cards(items, "No captures yet."), unsafe_allow_html=True)
        finally:
            conn.close()

# ---------------------------------------------------------------------------
# Corpus & reset -- admin actions, delegating to the real CLI scripts
# ---------------------------------------------------------------------------

elif section == "admin":
    with st.container(key="kv_import"):
        st.markdown(heading("Import a corpus"), unsafe_allow_html=True)
        uploaded = st.file_uploader("Corpus JSON", type=["json"])
        if uploaded and st.button("Run import", type="primary", key="kv_importbtn"):
            tmp_path = REPO_ROOT / "logs" / "_uploaded_corpus.json"
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(uploaded.getvalue())
            with st.spinner("Importing…"):
                proc = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "import_corpus.py"), str(tmp_path)],
                    capture_output=True, text=True, cwd=REPO_ROOT,
                )
            st.code(proc.stdout + proc.stderr or "(no output)")
            if proc.returncode == 0:
                st.success("Import finished.")
            else:
                st.error(f"import_corpus.py exited {proc.returncode}")

    st.markdown('<div class="kv-rule"></div>', unsafe_allow_html=True)

    with st.container(key="kv_reset"):
        st.markdown(
            heading("Reset database", "deletes all memory, cannot be undone", danger=True),
            unsafe_allow_html=True,
        )
        confirm = st.checkbox("I understand this deletes all memory")
        if st.button("Reset now", disabled=not confirm, key="kv_resetbtn"):
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "reset_db.py")],
                capture_output=True, text=True, cwd=REPO_ROOT,
            )
            st.code(proc.stdout + proc.stderr or "(no output)")
            if proc.returncode == 0:
                st.success("Database reset.")
                st.session_state.chat_history = []
            else:
                st.error(f"reset_db.py exited {proc.returncode}")
