#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import scholialang_mcp_server as scholia  # noqa: E402


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_POLL_SECONDS = 0.75
MAX_STREAM_SECONDS = 60 * 60 * 8
AST_CONNECTION_LIMIT = 180
INLINE_REF_RE = re.compile(r"\b(REFER|IMPLIES):([A-Za-z0-9_:-]+)")


WEBVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Scholialang Live</title>
  <script>
    (function () {
      try {
        document.documentElement.dataset.theme =
          localStorage.getItem("scholialang.webview.theme") === "light" ? "light" : "dark";
      } catch (_) {
        document.documentElement.dataset.theme = "dark";
      }
    }());
  </script>
  <script id="scholiaLiveConfig">window.__scholiaLiveConfig = __SCHOLIA_LIVE_CONFIG__;</script>
  <style>
    :root,
    :root[data-theme="dark"] {
      color-scheme: dark;
      --bg: #0f100d;
      --bg-2: #131611;
      --surface: #171a15;
      --surface-2: #1e231d;
      --surface-3: #272d26;
      --elev: #30382e;
      --border: #2a3128;
      --border-2: #394236;
      --text: #f2ecde;
      --text-2: #c8c0b1;
      --muted: #968d7e;
      --faint: #60584c;
      --amber: #d6a063;
      --amber-2: #e9bd87;
      --amber-soft: rgba(214,160,99,0.14);
      --sage: #8ca97f;
      --sage-soft: rgba(140,169,127,0.14);
      --danger: #d2856d;
      --danger-soft: rgba(210,133,109,0.14);
      --paper: #f2ead9;
      --paper-2: #e8dcc7;
      --mark-text: #16130e;
      --canvas: #13160f;
      --topbar-bg: rgba(15,16,13,0.88);
      --field-bg: rgba(255,255,255,0.02);
      --hover-bg: rgba(255,255,255,0.025);
      --active-bg: rgba(214,160,99,0.08);
      --active-border: rgba(214,160,99,0.22);
      --body-radial: radial-gradient(ellipse 120% 60% at 50% 0%, rgba(214,160,99,0.045), transparent 70%);
      --body-sheen: linear-gradient(180deg, rgba(255,255,255,0.016), transparent 220px);
      --panel-sheen: linear-gradient(180deg, rgba(255,255,255,0.024), transparent 46%);
      --panel-rule: rgba(255,255,255,0.03);
      --grid-line: rgba(255,255,255,0.028);
      --selection-bg: rgba(214,160,99,0.22);
      --focus-inner: rgba(15,16,13,0.95);
      --amber-glow: rgba(214,160,99,0.12);
      --sage-glow: rgba(140,169,127,0.14);
      --atom-bg: rgba(255,255,255,0.018);
      --chip-bg: rgba(255,255,255,0.02);
      --graph-node-fill: #171a15;
      --graph-text: #f2ecde;
      --graph-muted: #968d7e;
      --live-text: #cbe3bf;
      --danger-text: #efb19f;
      --loading-base: rgba(255,255,255,0.024);
      --loading-sheen: rgba(255,255,255,0.075);
      --ambient-warm: radial-gradient(circle at 10% -10%, rgba(214,160,99,0.12), transparent 28%);
      --ambient-cool: radial-gradient(circle at 100% 110%, rgba(140,169,127,0.12), transparent 34%);
      --cat-reasoning: #ff5e7a;
      --cat-evidence: #6ac9f2;
      --cat-control: #9be27d;
      --cat-reference: #ffa862;
      --cat-social: #c07dff;
      --cat-meta: #ffe16a;
      --cat-reasoning-soft: rgba(255,94,122,0.13);
      --cat-evidence-soft: rgba(106,201,242,0.13);
      --cat-control-soft: rgba(155,226,125,0.13);
      --cat-reference-soft: rgba(255,168,98,0.13);
      --cat-social-soft: rgba(192,125,255,0.13);
      --cat-meta-soft: rgba(255,225,106,0.13);
      --cat-reasoning-faint: rgba(255,94,122,0.055);
      --cat-evidence-faint: rgba(106,201,242,0.055);
      --cat-control-faint: rgba(155,226,125,0.055);
      --cat-reference-faint: rgba(255,168,98,0.055);
      --cat-social-faint: rgba(192,125,255,0.055);
      --cat-meta-faint: rgba(255,225,106,0.055);
      --cat-reasoning-border: rgba(255,94,122,0.32);
      --cat-evidence-border: rgba(106,201,242,0.32);
      --cat-control-border: rgba(155,226,125,0.32);
      --cat-reference-border: rgba(255,168,98,0.32);
      --cat-social-border: rgba(192,125,255,0.32);
      --cat-meta-border: rgba(255,225,106,0.32);
      --shadow:
        0 1px 0 rgba(255,255,255,0.015),
        0 1px 2px rgba(0,0,0,0.28),
        0 10px 24px -10px rgba(0,0,0,0.55),
        0 24px 48px -24px rgba(0,0,0,0.45);
      --sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }

    :root[data-theme="light"] {
      color-scheme: light;
      --bg: #fafaf9;
      --bg-2: #f5f4f1;
      --surface: #ffffff;
      --surface-2: #f5f4f1;
      --surface-3: #ece9e1;
      --elev: #e0dbcf;
      --border: #e8e5e0;
      --border-2: #d0c9bc;
      --text: #0a0a0a;
      --text-2: #555555;
      --muted: #77716a;
      --faint: #aaa29a;
      --amber: #b97024;
      --amber-2: #8f551c;
      --amber-soft: rgba(185,112,36,0.12);
      --sage: #4f7e59;
      --sage-soft: rgba(79,126,89,0.12);
      --danger: #a8533e;
      --danger-soft: rgba(168,83,62,0.12);
      --paper: #17140f;
      --paper-2: #2b261e;
      --mark-text: #f8f4ea;
      --canvas: #fafaf9;
      --topbar-bg: rgba(250,250,249,0.88);
      --field-bg: rgba(10,10,10,0.025);
      --hover-bg: rgba(10,10,10,0.04);
      --active-bg: rgba(185,112,36,0.09);
      --active-border: rgba(185,112,36,0.28);
      --body-radial: radial-gradient(ellipse 120% 60% at 50% 0%, rgba(214,160,99,0.12), transparent 70%);
      --body-sheen: linear-gradient(180deg, rgba(255,255,255,0.72), transparent 220px);
      --panel-sheen: linear-gradient(180deg, rgba(255,255,255,0.72), transparent 52%);
      --panel-rule: rgba(255,255,255,0.8);
      --grid-line: rgba(10,10,10,0.035);
      --selection-bg: rgba(185,112,36,0.22);
      --focus-inner: rgba(250,250,249,0.95);
      --amber-glow: rgba(185,112,36,0.13);
      --sage-glow: rgba(79,126,89,0.13);
      --atom-bg: rgba(255,255,255,0.76);
      --chip-bg: rgba(10,10,10,0.025);
      --graph-node-fill: #ffffff;
      --graph-text: #0a0a0a;
      --graph-muted: #77716a;
      --live-text: #355f3d;
      --danger-text: #8f402f;
      --loading-base: rgba(10,10,10,0.035);
      --loading-sheen: rgba(255,255,255,0.8);
      --ambient-warm: radial-gradient(circle at 10% -10%, rgba(185,112,36,0.12), transparent 28%);
      --ambient-cool: radial-gradient(circle at 100% 110%, rgba(79,126,89,0.12), transparent 34%);
      --shadow:
        0 1px 0 rgba(255,255,255,0.9),
        0 1px 2px rgba(16,14,10,0.05),
        0 12px 28px -18px rgba(16,14,10,0.25);
    }

    * { box-sizing: border-box; }

    html {
      height: 100%;
      background: var(--bg);
      scroll-padding-top: calc(72px + env(safe-area-inset-top, 0px));
      -webkit-text-size-adjust: 100%;
      text-size-adjust: 100%;
    }

    body {
      margin: 0;
      min-height: 100vh;
      min-height: 100dvh;
      font: 13px/1.45 var(--sans);
      color: var(--text);
      background:
        var(--ambient-warm),
        var(--ambient-cool),
        var(--body-radial),
        var(--body-sheen),
        var(--canvas);
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
      overscroll-behavior: none;
      overflow-x: hidden;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.045;
      mix-blend-mode: overlay;
      background-image:
        linear-gradient(var(--grid-line) 1px, transparent 1px),
        linear-gradient(90deg, var(--grid-line) 1px, transparent 1px);
      background-size: 22px 22px;
      background-position: -1px -1px;
    }

    ::selection {
      background: var(--selection-bg);
      color: var(--text);
    }

    @keyframes scholia-live-pulse {
      0%, 100% {
        box-shadow: 0 0 0 4px var(--sage-glow), 0 0 0 0 rgba(140,169,127,0.18);
      }
      50% {
        box-shadow: 0 0 0 4px var(--sage-glow), 0 0 0 7px rgba(140,169,127,0.07);
      }
    }

    @keyframes scholia-sync-pulse {
      0%, 100% {
        box-shadow: 0 0 0 4px var(--amber-glow), 0 0 0 0 rgba(214,160,99,0.18);
      }
      50% {
        box-shadow: 0 0 0 4px var(--amber-glow), 0 0 0 7px rgba(214,160,99,0.08);
      }
    }

    @keyframes scholia-spin {
      to { transform: rotate(360deg); }
    }

    @keyframes scholia-row-enter {
      from {
        opacity: 0;
        transform: translateY(5px);
        filter: blur(3px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
        filter: blur(0);
      }
    }

    @keyframes scholia-trace-enter {
      from {
        opacity: 0.42;
        transform: translateY(6px);
        filter: blur(4px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
        filter: blur(0);
      }
    }

    @keyframes scholia-panel-rise {
      from {
        opacity: 0;
        transform: translateY(8px) scale(0.995);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }

    @keyframes scholia-active-sheen {
      from { transform: translateX(-110%); }
      to { transform: translateX(110%); }
    }

    @keyframes scholia-incoming-glow {
      0% {
        border-color: var(--atom-color);
        box-shadow:
          inset 0 1px 0 rgba(255,255,255,0.04),
          0 0 0 1px var(--atom-border),
          0 0 0 8px var(--atom-faint);
      }
      100% {
        border-color: var(--border);
        box-shadow: none;
      }
    }

    @keyframes scholia-skeleton-sheen {
      from { transform: translateX(-120%); }
      to { transform: translateX(120%); }
    }

    @keyframes scholia-graph-edge {
      from { stroke-dashoffset: 16; opacity: 0.2; }
      to { stroke-dashoffset: 0; opacity: 1; }
    }

    @keyframes scholia-graph-node {
      from {
        opacity: 0;
        transform: translateY(4px) scale(0.98);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }

    button, select {
      font: inherit;
      color: inherit;
    }

    .app {
      display: grid;
      grid-template-rows: auto 1fr;
      min-height: 100vh;
    }

    .topbar {
      display: flex;
      align-items: center;
      gap: 16px;
      min-height: 64px;
      padding:
        calc(12px + env(safe-area-inset-top, 0px))
        calc(18px + env(safe-area-inset-right, 0px))
        12px
        calc(18px + env(safe-area-inset-left, 0px));
      border-bottom: 1px solid var(--border);
      background: var(--topbar-bg);
      backdrop-filter: blur(18px);
      position: sticky;
      top: 0;
      z-index: 5;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      flex: 0 0 auto;
      font-weight: 650;
      letter-spacing: 0;
    }

    .mark {
      width: 34px;
      height: 34px;
      border-radius: 2px;
      background: var(--paper);
      color: var(--mark-text);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.18);
    }

    .mark::after {
      content: "OT";
      font: 760 12px/1 var(--sans);
      letter-spacing: 0;
    }

    .toolbar {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      row-gap: 8px;
      flex: 1;
      min-width: 0;
      max-width: 100%;
    }

    .toolbar select {
      height: 36px;
      min-width: 0;
      padding: 0 32px 0 10px;
      border: 1px solid var(--border);
      border-radius: 2px;
      background: var(--field-bg);
      color: var(--text);
      transition-property: background-color, border-color, box-shadow, transform;
      transition-duration: 0.16s;
      transition-timing-function: ease;
    }

    .toolbar #dagSelect {
      min-width: 190px;
      max-width: min(260px, 26vw);
    }

    .toolbar #projectSelect {
      min-width: 150px;
      max-width: min(300px, 30vw);
    }

    #scopeToggle {
      min-width: 76px;
    }

    .icon-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 36px;
      min-width: 36px;
      padding: 0;
    }

    .button svg,
    .view-toggle-button svg {
      width: 15px;
      height: 15px;
    }

    .view-toggle-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }

    /* Theme toggle: exactly ONE glyph visible, driven by the active theme.
       Scoped to #themeToggle so it always wins over generic svg sizing rules. */
    #themeToggle .icon-sun,
    #themeToggle .icon-moon { display: none; }
    :root[data-theme="light"] #themeToggle .icon-sun { display: block; }
    :root[data-theme="dark"] #themeToggle .icon-moon { display: block; }

    .view-toggle {
      display: inline-grid;
      grid-template-columns: 1fr 1fr;
      min-width: 180px;
      height: 36px;
      padding: 2px;
      border: 1px solid var(--border);
      border-radius: 2px;
      background: var(--field-bg);
    }

    .view-toggle-button,
    .order-toggle-button {
      min-width: 0;
      min-height: 30px;
      border: 0;
      border-radius: 1px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font: 650 11px/1 var(--mono);
      font-variant-numeric: tabular-nums;
      transition-property: background-color, color, transform, opacity;
      transition-duration: 0.16s;
      transition-timing-function: ease;
    }

    .view-toggle-button:hover:not(:disabled),
    .order-toggle-button:hover:not(:disabled) {
      color: var(--text);
      background: var(--hover-bg);
    }

    .view-toggle-button:active:not(:disabled),
    .order-toggle-button:active:not(:disabled) {
      transform: scale(0.96);
    }

    .view-toggle-button.active,
    .order-toggle-button.active {
      color: var(--text);
      background: var(--active-bg);
      box-shadow: inset 0 0 0 1px var(--active-border);
    }

    .view-toggle-button:disabled {
      opacity: 0.42;
      cursor: not-allowed;
    }

    .button {
      height: 36px;
      min-width: 36px;
      padding: 0 11px;
      border: 1px solid var(--border);
      border-radius: 2px;
      background: var(--surface-2);
      color: var(--text);
      cursor: pointer;
      transition-property: background-color, border-color, box-shadow, transform;
      transition-duration: 0.16s;
      transition-timing-function: ease;
    }

    .button-label-short {
      display: none;
    }

    .button:hover {
      border-color: var(--border-2);
      background: var(--surface-3);
      transform: translateY(-1px);
    }

    .button:active {
      transform: scale(0.96);
    }

    .button:focus-visible,
    .view-toggle-button:focus-visible,
    .order-toggle-button:focus-visible,
    select:focus-visible,
    .dag-item:focus-visible {
      outline: none;
      box-shadow:
        0 0 0 2px var(--focus-inner),
        0 0 0 4px rgba(214,160,99,0.72);
    }

    .status {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 28px;
      padding: 0 9px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.026), transparent 70%),
        rgba(255,255,255,0.012);
      color: var(--muted);
      white-space: nowrap;
      font-size: 12px;
      font-family: var(--mono);
      font-variant-numeric: tabular-nums;
      transition-property: background-color, border-color, color, box-shadow;
      transition-duration: 0.18s;
      transition-timing-function: ease;
    }

    .status-dot {
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--amber);
      box-shadow: 0 0 0 4px var(--amber-glow);
    }

    .status[data-mode="loading"],
    .status[data-mode="syncing"],
    .status[data-mode="reconnecting"] {
      border-color: rgba(214,160,99,0.28);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.026), transparent 70%),
        var(--amber-soft);
      color: var(--amber-2);
    }

    .status[data-mode="live"] {
      border-color: rgba(140,169,127,0.22);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.026), transparent 70%),
        var(--sage-soft);
      color: var(--live-text);
    }

    .status[data-mode="error"] {
      border-color: rgba(210,133,109,0.25);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.026), transparent 70%),
        var(--danger-soft);
      color: var(--danger-text);
    }

    .status-dot.loading,
    .status-dot.syncing,
    .status-dot.reconnecting {
      animation: scholia-sync-pulse 1.35s ease-in-out infinite;
    }

    .status-dot.live {
      background: var(--sage);
      box-shadow: 0 0 0 4px var(--sage-glow);
      animation: scholia-live-pulse 1.8s ease-in-out infinite;
    }

    .status-dot.error {
      background: var(--danger);
      box-shadow: 0 0 0 4px var(--danger-soft);
    }

    .status-spinner {
      width: 12px;
      height: 12px;
      border: 1.6px solid currentColor;
      border-right-color: transparent;
      border-radius: 999px;
      animation: scholia-spin 0.9s linear infinite;
    }

    .status-spinner[hidden] {
      display: none;
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(0, 300px) minmax(0, 1fr) minmax(0, 1fr);
      gap: 14px;
      width: 100%;
      max-width: 100%;
      padding:
        14px
        max(14px, env(safe-area-inset-right, 0px))
        calc(14px + env(safe-area-inset-bottom, 0px))
        max(14px, env(safe-area-inset-left, 0px));
      min-height: 0;
    }

    .layout > *,
    .side-stack,
    .side-stack > * {
      min-width: 0;
    }

    .panel {
      min-height: 0;
      min-width: 0;
      border: 1px solid var(--border);
      border-radius: 2px;
      background:
        var(--panel-sheen),
        var(--surface);
      box-shadow: var(--shadow);
      overflow: hidden;
      position: relative;
      transition-property: border-color, box-shadow, transform;
      transition-duration: 0.18s;
      transition-timing-function: ease;
      animation: scholia-panel-rise 320ms cubic-bezier(0.2, 0, 0, 1) both;
      animation-delay: calc(var(--panel-index, 0) * 38ms);
    }

    .layout > .panel:nth-child(1) { --panel-index: 0; }
    .layout > .panel:nth-child(2) { --panel-index: 1; }
    .side-stack > .panel:nth-child(1) { --panel-index: 2; }
    .side-stack > .panel:nth-child(2) { --panel-index: 3; }
    .side-stack > .panel:nth-child(3) { --panel-index: 4; }
    .side-stack > .panel:nth-child(4) { --panel-index: 5; }

    .panel::after {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      box-shadow: inset 0 1px 0 var(--panel-rule);
    }

    .panel.is-busy::before {
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      top: 0;
      height: 2px;
      z-index: 2;
      background: linear-gradient(90deg, transparent, var(--amber), transparent);
      animation: scholia-skeleton-sheen 1.2s ease-in-out infinite;
    }

    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 44px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      background: var(--surface-2);
    }

    .panel-title {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 650;
      letter-spacing: 0;
      text-transform: uppercase;
    }

    .panel-meta {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      font-variant-numeric: tabular-nums;
    }

    .panel-body {
      overflow: auto;
      max-height: calc(100vh - 94px);
      min-width: 0;
      -webkit-overflow-scrolling: touch;
    }

    .dag-list {
      display: grid;
      gap: 6px;
      padding: 10px;
      min-width: 0;
    }

    .dag-item {
      width: 100%;
      min-width: 0;
      border: 1px solid transparent;
      border-radius: 2px;
      background: transparent;
      padding: 9px;
      text-align: left;
      cursor: pointer;
      overflow: hidden;
      position: relative;
      transition: background-color 0.16s ease, border-color 0.16s ease, transform 0.16s ease;
    }

    .dag-item:hover {
      background: var(--hover-bg);
      transform: translateY(-1px);
    }

    .dag-item.active {
      border-color: var(--active-border);
      background: var(--active-bg);
    }

    .dag-item.active::after {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(100deg, transparent, rgba(255,255,255,0.055), transparent);
      transform: translateX(-110%);
      animation: scholia-active-sheen 820ms ease-out both;
    }

    .dag-title {
      color: var(--text);
      font-weight: 560;
      overflow-wrap: anywhere;
    }

    .dag-sub {
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }

    .feed {
      display: grid;
      gap: 8px;
      padding: 10px;
      min-width: 0;
    }

    .atom,
    .frontier-item,
    .connection-node {
      --atom-color: var(--cat-reference);
      --atom-soft: var(--cat-reference-soft);
      --atom-faint: var(--cat-reference-faint);
      --atom-border: var(--cat-reference-border);
    }

    .atom[data-cat="reasoning"],
    .frontier-item[data-cat="reasoning"],
    .connection-node[data-cat="reasoning"] {
      --atom-color: var(--cat-reasoning);
      --atom-soft: var(--cat-reasoning-soft);
      --atom-faint: var(--cat-reasoning-faint);
      --atom-border: var(--cat-reasoning-border);
    }

    .atom[data-cat="evidence"],
    .frontier-item[data-cat="evidence"],
    .connection-node[data-cat="evidence"] {
      --atom-color: var(--cat-evidence);
      --atom-soft: var(--cat-evidence-soft);
      --atom-faint: var(--cat-evidence-faint);
      --atom-border: var(--cat-evidence-border);
    }

    .atom[data-cat="control"],
    .frontier-item[data-cat="control"],
    .connection-node[data-cat="control"] {
      --atom-color: var(--cat-control);
      --atom-soft: var(--cat-control-soft);
      --atom-faint: var(--cat-control-faint);
      --atom-border: var(--cat-control-border);
    }

    .atom[data-cat="social"],
    .frontier-item[data-cat="social"],
    .connection-node[data-cat="social"] {
      --atom-color: var(--cat-social);
      --atom-soft: var(--cat-social-soft);
      --atom-faint: var(--cat-social-faint);
      --atom-border: var(--cat-social-border);
    }

    .atom[data-cat="meta"],
    .frontier-item[data-cat="meta"],
    .connection-node[data-cat="meta"] {
      --atom-color: var(--cat-meta);
      --atom-soft: var(--cat-meta-soft);
      --atom-faint: var(--cat-meta-faint);
      --atom-border: var(--cat-meta-border);
    }

    .atom {
      display: grid;
      grid-template-columns: 104px minmax(0, 1fr);
      min-width: 0;
      gap: 10px;
      padding: 10px;
      border: 1px solid var(--border);
      border-left: 3px solid var(--atom-color);
      border-radius: 2px;
      background:
        linear-gradient(90deg, var(--atom-faint), transparent 56%),
        var(--atom-bg);
      transition-property: background-color, border-color, box-shadow, transform;
      transition-duration: 0.16s;
      transition-timing-function: ease;
    }

    .atom:hover {
      transform: translateY(-1px);
    }

    .atom.is-new {
      animation:
        scholia-row-enter 260ms cubic-bezier(0.2, 0, 0, 1) both,
        scholia-incoming-glow 1200ms ease-out both;
      animation-delay: var(--enter-delay, 0ms), var(--enter-delay, 0ms);
    }

    .kind-row {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
    }

    .kind {
      width: fit-content;
      max-width: 100%;
      padding: 4px 7px;
      border-radius: 999px;
      border: 1px solid var(--border);
      font-family: var(--mono);
      font-size: 10.5px;
      font-weight: 650;
      color: var(--atom-color);
      background: var(--atom-soft);
      border-color: var(--atom-border);
      overflow-wrap: anywhere;
    }

    .category-chip {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0;
    }

    .category-chip::before {
      content: "";
      display: inline-block;
      width: 6px;
      height: 6px;
      margin-right: 5px;
      border-radius: 999px;
      background: var(--atom-color);
      vertical-align: 1px;
    }

    .atom-id {
      margin-top: 7px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10.5px;
      overflow-wrap: anywhere;
    }

    .atom-summary {
      color: var(--text);
      font-weight: 560;
      overflow-wrap: anywhere;
    }

    .atom-content {
      margin-top: 6px;
      color: var(--text-2);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .files {
      margin-top: 8px;
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }

    .file-chip {
      max-width: 100%;
      padding: 3px 6px;
      border: 1px solid var(--border);
      border-radius: 999px;
      color: var(--muted);
      background: var(--chip-bg);
      font-family: var(--mono);
      font-size: 11px;
      overflow-wrap: anywhere;
    }

    .side-stack {
      display: grid;
      gap: 14px;
      align-content: start;
      min-width: 0;
    }

    .summary {
      padding: 10px 12px;
      color: var(--text-2);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.5;
      min-width: 0;
    }

    .frontier {
      display: grid;
      gap: 7px;
      padding: 10px;
      min-width: 0;
    }

    .frontier-item {
      border: 1px solid var(--border);
      border-left: 3px solid var(--atom-color);
      border-radius: 2px;
      padding: 8px;
      background:
        linear-gradient(90deg, var(--atom-faint), transparent 58%),
        var(--atom-bg);
      transition-property: background-color, border-color, box-shadow, transform;
      transition-duration: 0.16s;
      transition-timing-function: ease;
    }

    .frontier-item.is-new {
      animation:
        scholia-row-enter 240ms cubic-bezier(0.2, 0, 0, 1) both,
        scholia-incoming-glow 1000ms ease-out both;
      animation-delay: var(--enter-delay, 0ms), var(--enter-delay, 0ms);
    }

    .graph-wrap {
      overflow: auto;
      padding: 8px;
      min-width: 0;
      -webkit-overflow-scrolling: touch;
      background:
        linear-gradient(var(--grid-line) 1px, transparent 1px),
        linear-gradient(90deg, var(--grid-line) 1px, transparent 1px),
        var(--surface);
      background-size: 22px 22px;
    }

    .graph {
      min-width: 100%;
      height: 220px;
    }

    .graph-edge.is-new {
      stroke-dasharray: 8 8;
      animation: scholia-graph-edge 420ms cubic-bezier(0.2, 0, 0, 1) both;
    }

    .graph-node.is-new {
      transform-box: fill-box;
      transform-origin: center;
      animation: scholia-graph-node 260ms cubic-bezier(0.2, 0, 0, 1) both;
    }

    .connection-list {
      display: grid;
      gap: 7px;
      padding: 10px;
      max-height: 340px;
      overflow: auto;
      min-width: 0;
      -webkit-overflow-scrolling: touch;
    }

    .connection-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(82px, auto) minmax(0, 1fr);
      gap: 8px;
      align-items: stretch;
      transition-property: opacity, transform;
      transition-duration: 0.16s;
      transition-timing-function: ease;
    }

    .connection-row.is-new {
      animation: scholia-row-enter 240ms cubic-bezier(0.2, 0, 0, 1) both;
      animation-delay: var(--enter-delay, 0ms);
    }

    .trace-region {
      transition-property: opacity, transform, filter;
      transition-duration: 0.18s;
      transition-timing-function: cubic-bezier(0.2, 0, 0, 1);
      will-change: opacity, transform, filter;
    }

    .trace-region.is-switching {
      opacity: 0.52;
      transform: translateY(3px);
      filter: blur(1.2px);
      pointer-events: none;
    }

    .trace-region.is-trace-entering {
      animation: scholia-trace-enter 320ms cubic-bezier(0.2, 0, 0, 1) both;
    }

    .connection-node {
      min-width: 0;
      border: 1px solid var(--border);
      border-left: 3px solid var(--atom-color);
      border-radius: 2px;
      padding: 7px 8px;
      background:
        linear-gradient(90deg, var(--atom-faint), transparent 62%),
        var(--atom-bg);
      transition-property: background-color, border-color, box-shadow;
      transition-duration: 0.16s;
      transition-timing-function: ease;
    }

    .connection-row.is-new .connection-node {
      animation: scholia-incoming-glow 1000ms ease-out both;
      animation-delay: var(--enter-delay, 0ms);
    }

    .connection-label {
      color: var(--text);
      font-weight: 560;
      overflow-wrap: anywhere;
    }

    .connection-detail {
      margin-top: 3px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10.5px;
      overflow-wrap: anywhere;
    }

    .connection-relation {
      display: flex;
      min-width: 0;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 4px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10.5px;
      text-align: center;
    }

    .connection-relation::before,
    .connection-relation::after {
      content: "";
      width: 100%;
      height: 1px;
      background: var(--border-2);
    }

    .connection-relation span {
      max-width: 100%;
      padding: 2px 6px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--chip-bg);
      color: var(--text-2);
      overflow-wrap: anywhere;
    }

    .connection-origin {
      color: var(--faint);
      font-size: 10px;
      text-transform: uppercase;
    }

    .empty {
      padding: 22px 12px;
      color: var(--muted);
      text-align: center;
    }

    .loading-stage {
      display: grid;
      gap: 8px;
      padding: 10px;
    }

    .loading-copy {
      display: inline-flex;
      width: fit-content;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--border);
      border-radius: 2px;
      background: var(--field-bg);
      color: var(--muted);
      padding: 6px 8px;
      font: 500 11px/1 var(--sans);
    }

    .loading-copy .status-spinner {
      width: 13px;
      height: 13px;
    }

    .skeleton-card,
    .skeleton-line {
      position: relative;
      overflow: hidden;
      border-radius: 2px;
      background: var(--loading-base);
    }

    .skeleton-card {
      min-height: 68px;
      border: 1px solid var(--border);
      padding: 10px;
    }

    .skeleton-line {
      height: 9px;
      margin-top: 8px;
    }

    .skeleton-line:first-child {
      margin-top: 0;
    }

    .skeleton-line.short {
      width: 42%;
    }

    .skeleton-line.medium {
      width: 68%;
    }

    .skeleton-line.long {
      width: 88%;
    }

    .skeleton-card::after,
    .skeleton-line::after {
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg, transparent, var(--loading-sheen), transparent);
      transform: translateX(-120%);
      animation: scholia-skeleton-sheen 1.35s ease-in-out infinite;
    }

    .summary.loading-stage,
    .frontier.loading-stage {
      padding: 10px;
    }

    .graph-placeholder {
      height: 220px;
      display: grid;
      place-items: center;
      color: var(--muted);
      font: 500 11px/1 var(--mono);
    }

    @media (prefers-reduced-motion: reduce) {
      *,
      *::before,
      *::after {
        animation-duration: 1ms !important;
        animation-iteration-count: 1 !important;
        scroll-behavior: auto !important;
      }

      .button:hover,
      .dag-item:hover,
      .atom:hover,
      .trace-region.is-switching {
        transform: none;
        filter: none;
      }
    }

    @media (hover: none) and (pointer: coarse) {
      .toolbar select,
      .button,
      .dag-item {
        min-height: 44px;
      }

      .button:hover,
      .dag-item:hover,
      .atom:hover,
      .frontier-item:hover {
        transform: none;
      }
    }

    @media (max-width: 1120px) {
      .layout {
        grid-template-columns: minmax(0, 260px) minmax(0, 1fr);
      }
      .side-stack {
        grid-column: 1 / -1;
        grid-template-columns: 1fr 1fr;
      }
      .panel-body {
        max-height: none;
      }
    }

    @media (max-width: 760px) {
      html {
        scroll-padding-top: calc(154px + env(safe-area-inset-top, 0px));
      }

      body {
        overscroll-behavior-y: auto;
      }

      .topbar {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: center;
        gap: 10px;
        min-height: 0;
        padding:
          calc(10px + env(safe-area-inset-top, 0px))
          calc(10px + env(safe-area-inset-right, 0px))
          10px
          calc(10px + env(safe-area-inset-left, 0px));
      }

      .brand {
        grid-column: 1;
        grid-row: 1;
        min-width: 0;
      }

      .brand span {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .mark {
        width: 32px;
        height: 32px;
        flex: 0 0 auto;
      }

      .toolbar {
        display: grid;
        grid-column: 1 / -1;
        grid-row: 2;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
        gap: 8px;
        width: 100%;
        min-width: 0;
      }

      .toolbar select {
        width: 100%;
        max-width: 100%;
        height: 44px;
        min-height: 44px;
        font-size: 16px;
      }

      .toolbar #dagSelect {
        grid-column: 1 / -1;
        min-width: 0;
        max-width: 100%;
      }

      .toolbar #projectSelect {
        grid-column: 1 / -1;
        max-width: 100%;
      }

      #scopeToggle {
        grid-column: 1 / -1;
      }

      .view-toggle {
        grid-column: 1 / 3;
        width: 100%;
        min-width: 0;
        height: 44px;
      }

      .view-toggle-button,
      .order-toggle-button {
        min-height: 38px;
        font-size: 11px;
      }

      .toolbar #themeSelect {
        min-width: 0;
        max-width: 100%;
      }

      .button {
        height: 44px;
        min-width: 44px;
        padding: 0 12px;
        font-size: 13px;
      }

      .button-label-full {
        display: none;
      }

      .button-label-short {
        display: inline;
      }

      .status {
        grid-column: 2;
        grid-row: 1;
        justify-self: end;
        max-width: min(42vw, 150px);
        min-height: 32px;
        padding: 0 8px;
        overflow: hidden;
      }

      #statusText {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .layout,
      .side-stack {
        grid-template-columns: minmax(0, 1fr);
        gap: 10px;
      }

      .layout {
        padding:
          10px
          calc(10px + env(safe-area-inset-right, 0px))
          calc(18px + env(safe-area-inset-bottom, 0px))
          calc(10px + env(safe-area-inset-left, 0px));
      }

      .panel-head {
        min-height: 40px;
        padding: 8px 10px;
      }

      .panel-title,
      .panel-meta {
        font-size: 10.5px;
      }

      .panel-body {
        max-height: none;
      }

      .dag-list {
        max-height: min(42dvh, 360px);
        overflow: auto;
        padding: 8px;
      }

      .dag-item {
        padding: 10px;
      }

      .atom {
        grid-template-columns: minmax(0, 1fr);
        gap: 8px;
        padding: 10px;
      }

      .atom-id {
        margin-top: 5px;
      }

      .feed,
      .frontier,
      .connection-list {
        padding: 8px;
      }

      .summary {
        max-height: 44dvh;
        overflow: auto;
        padding: 9px 10px;
      }

      .graph-wrap {
        padding: 6px;
      }

      .graph {
        min-width: 720px;
      }

      .connection-list {
        max-height: 42dvh;
      }

      .connection-row {
        grid-template-columns: minmax(0, 1fr);
      }
      .connection-relation::before,
      .connection-relation::after {
        width: 1px;
        height: 10px;
      }
    }

    @media (max-width: 420px) {
      .topbar {
        gap: 8px;
      }

      .brand {
        gap: 8px;
      }

      .brand span {
        max-width: 168px;
      }

      .status {
        max-width: 124px;
        font-size: 11px;
      }

      .status-spinner {
        width: 11px;
        height: 11px;
      }

      .dag-sub,
      .atom-id,
      .connection-detail {
        font-size: 10px;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand"><div class="mark" aria-hidden="true"></div><span>Scholialang Live</span></div>
      <div class="toolbar">
        <div id="scopeToggle" class="view-toggle" role="group" aria-label="Project scope">
          <button type="button" class="view-toggle-button" data-scope="project" aria-label="This project" title="This project — show only the current project"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg></button>
          <button type="button" class="view-toggle-button" data-scope="all" aria-label="All projects" title="All projects — blend traces across all projects"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg></button>
        </div>
        <select id="projectSelect" aria-label="Project"></select>
        <select id="dagSelect" aria-label="DAG"></select>
        <div id="viewToggle" class="view-toggle" role="group" aria-label="Trace view">
          <button type="button" class="view-toggle-button" data-view-mode="checkpoint">Checkpoint</button>
          <button type="button" class="view-toggle-button" data-view-mode="exhaust">Exhaust</button>
        </div>
        <div id="orderToggle" class="view-toggle" role="group" aria-label="Atom feed order">
          <button type="button" class="order-toggle-button" data-feed-order="newest" title="Newest atoms at the top">Newest first</button>
          <button type="button" class="order-toggle-button" data-feed-order="oldest" title="Chronological; new atoms appear at the bottom">Oldest first</button>
        </div>
        <button id="themeToggle" type="button" class="button icon-button" aria-label="Toggle theme" title="Toggle light / dark theme">
          <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
          <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
        </button>
        <button id="refresh" class="button" title="Refresh"><span class="button-label-full">Refresh</span><span class="button-label-short">Sync</span></button>
      </div>
      <div id="status" class="status" data-mode="loading" aria-live="polite">
        <span id="statusDot" class="status-dot loading"></span>
        <span id="statusSpinner" class="status-spinner" aria-hidden="true"></span>
        <span id="statusText">Connecting</span>
      </div>
    </header>

    <main class="layout">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">DAGs</div>
          <div id="dagCount" class="panel-meta">0</div>
        </div>
        <div id="dagList" class="panel-body dag-list"></div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">Atoms</div>
          <div id="atomCount" class="panel-meta">0</div>
        </div>
        <div id="feed" class="panel-body feed trace-region"></div>
      </section>

      <aside class="side-stack">
        <section class="panel">
          <div class="panel-head">
            <div class="panel-title">Frontier</div>
            <div id="frontierCount" class="panel-meta">0</div>
          </div>
          <div id="frontier" class="frontier trace-region"></div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div class="panel-title">Graph</div>
            <div id="edgeCount" class="panel-meta">0</div>
          </div>
          <div class="graph-wrap"><svg id="graph" class="graph trace-region" role="img" aria-label="DAG graph"></svg></div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div class="panel-title">AST Connections</div>
            <div id="astCount" class="panel-meta">0</div>
          </div>
          <div id="astConnections" class="connection-list trace-region"></div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div class="panel-title">Summary</div>
            <div id="updatedAt" class="panel-meta"></div>
          </div>
          <pre id="summary" class="summary trace-region"></pre>
        </section>
      </aside>
    </main>
  </div>

  <script>
    const STORAGE_THEME_KEY = "scholialang.webview.theme";
    const STORAGE_FEED_ORDER_KEY = "scholialang.webview.feedOrder";
    const STORAGE_SCOPE_KEY = "scholialang.webview.scope";
    const VALID_SCOPES = ["project", "all"];
    const DEFAULT_SCOPE = "project";
    const PROJECT_POLL_MS = 5000;
    const LIVE_CONFIG = (window && window.__scholiaLiveConfig) || {};
    const CATEGORY_ORDER = ["reasoning", "evidence", "control", "reference", "social", "meta"];
    const TRACE_REGION_IDS = ["feed", "frontier", "graph", "astConnections", "summary"];
    const CATEGORY_LABELS = {
      reasoning: "Reasoning",
      evidence: "Evidence",
      control: "Control",
      reference: "Reference",
      social: "Social",
      meta: "Meta",
    };
    const CATEGORY_FALLBACKS = {
      reasoning: "#ff5e7a",
      evidence: "#6ac9f2",
      control: "#9be27d",
      reference: "#ffa862",
      social: "#c07dff",
      meta: "#ffe16a",
    };
    const KIND_CATEGORY = {
      Thinking: "reasoning",
      Observation: "reasoning",
      Action: "reasoning",
      Hypothesis: "evidence",
      Evidence: "evidence",
      Finding: "evidence",
      Concluding: "evidence",
      Contradiction: "evidence",
      Uncertainty: "evidence",
      Retract: "evidence",
      Deciding: "control",
      Alternative: "control",
      Branch: "control",
      Loop: "control",
      Parallel: "control",
      Storing: "reference",
      Print: "reference",
      Reference: "reference",
      Implication: "reference",
      Handoff: "social",
      Question: "social",
      Review: "social",
      Constraint: "meta",
      Goal: "meta",
      Confidence: "meta",
      EventRef: "meta",
      Budget: "meta",
      Cost: "meta",
      Meta: "meta",
      Edge: "reference",
      Effect: "meta",
      Ref: "reference",
    };
    const searchParams = new URLSearchParams(location.search);
    const initialTheme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
    let initialFeedOrder = "newest";
    try {
      if (localStorage.getItem(STORAGE_FEED_ORDER_KEY) === "oldest") initialFeedOrder = "oldest";
    } catch (_) {
      // Local storage can be disabled; fall back to the default order.
    }
    const state = {
      dagId: searchParams.get("dag_id") || null,
      eventSource: null,
      feedOrder: initialFeedOrder,
      hasRenderedSnapshot: false,
      incomingTimer: null,
      newNodeIds: new Set(),
      scope: DEFAULT_SCOPE,
      currentProjectPath: "",
      projectPath: searchParams.get("project_path") || "",
      projects: [],
      projectPollTimer: null,
      recentWindowSecs: typeof LIVE_CONFIG.recent_window_secs === "number" ? LIVE_CONFIG.recent_window_secs : 300,
      seenNodeIds: new Set(),
      snapshot: null,
      snapshotController: null,
      snapshotRetryDelay: 1000,
      snapshotRetryTimer: null,
      theme: initialTheme,
      traceToken: 0,
      transitionTimer: null,
      streamToken: 0,
    };

    // URL params are authoritative on load — this fixes the stale-localStorage
    // "stuck project" bug. project_path in the URL is the *identity* of the
    // current project; scope (project vs all) follows the documented precedence
    // (URL ?scope= > saved UI choice > SCHOLIA_LIVE_SCOPE env > 'project').
    (function initScope() {
      const urlHasProject = searchParams.has("project_path");
      const urlProject = searchParams.get("project_path");
      state.currentProjectPath = urlHasProject && urlProject ? urlProject : "";
      state.scope = resolveInitialScope(urlHasProject, urlProject);
      state.projectPath = state.scope === "all"
        ? state.currentProjectPath
        : (urlHasProject && urlProject ? urlProject : state.currentProjectPath);
    }());
    const $ = (id) => document.getElementById(id);

    function setBusy(isBusy) {
      document.documentElement.dataset.streamState = isBusy ? "syncing" : "idle";
      document.querySelectorAll(".panel").forEach((panel) => {
        panel.classList.toggle("is-busy", Boolean(isBusy));
        panel.toggleAttribute("aria-busy", Boolean(isBusy));
      });
    }

    function traceRegions() {
      return TRACE_REGION_IDS.map((id) => $(id)).filter(Boolean);
    }

    function setTraceSwitching(isSwitching) {
      traceRegions().forEach((region) => {
        region.classList.toggle("is-switching", Boolean(isSwitching));
        region.toggleAttribute("aria-busy", Boolean(isSwitching));
      });
    }

    function runTraceEnterAnimation() {
      if (state.transitionTimer) window.clearTimeout(state.transitionTimer);
      traceRegions().forEach((region) => {
        region.classList.remove("is-trace-entering");
        void region.offsetWidth;
        region.classList.add("is-trace-entering");
      });
      state.transitionTimer = window.setTimeout(() => {
        traceRegions().forEach((region) => region.classList.remove("is-trace-entering"));
      }, 360);
    }

    function cssValue(name, fallback) {
      const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return value || fallback;
    }

    function categoryForKind(kind) {
      return KIND_CATEGORY[String(kind || "").trim()] || "reference";
    }

    function categoryLabel(category) {
      return CATEGORY_LABELS[category] || CATEGORY_LABELS.reference;
    }

    function categoryColor(category) {
      return cssValue(`--cat-${category}`, CATEGORY_FALLBACKS[category] || CATEGORY_FALLBACKS.reference);
    }

    function themeColor(name, fallback) {
      return cssValue(name, fallback);
    }

    function applyTheme(theme) {
      const nextTheme = theme === "light" ? "light" : "dark";
      state.theme = nextTheme;
      document.documentElement.dataset.theme = nextTheme;
      const themeBtn = $("themeToggle");
      if (themeBtn) {
        const toLight = nextTheme === "dark";
        themeBtn.setAttribute("aria-label", toLight ? "Switch to light theme" : "Switch to dark theme");
        themeBtn.title = toLight ? "Dark theme — switch to light" : "Light theme — switch to dark";
      }
      try {
        localStorage.setItem(STORAGE_THEME_KEY, nextTheme);
      } catch (_) {
        // Local storage can be disabled; the visible control still updates.
      }
      if (state.snapshot) renderGraph(state.snapshot.nodes || [], state.snapshot.edges || []);
    }

    function applyFeedOrder(order) {
      const nextOrder = order === "oldest" ? "oldest" : "newest";
      state.feedOrder = nextOrder;
      document.querySelectorAll("#orderToggle .order-toggle-button").forEach((button) => {
        const isActive = button.dataset.feedOrder === nextOrder;
        button.classList.toggle("active", isActive);
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
      });
      try {
        localStorage.setItem(STORAGE_FEED_ORDER_KEY, nextOrder);
      } catch (_) {
        // Local storage can be disabled; the visible control still updates.
      }
      if (state.snapshot) {
        renderAtoms(state.snapshot.nodes || []);
        const feed = $("feed");
        if (feed) feed.scrollTop = 0;
      }
    }

    function params(extra = {}) {
      const value = new URLSearchParams();
      if (state.scope === "all") {
        // Present-but-empty project_path => blended all-projects view server-side.
        value.set("project_path", "");
      } else if (state.projectPath) {
        value.set("project_path", state.projectPath);
      }
      // scope=project with no concrete project: omit the param so the server
      // falls back to its launch-dir default (byte-identical legacy behavior).
      for (const [key, item] of Object.entries(extra)) {
        if (item !== undefined && item !== null && item !== "") value.set(key, item);
      }
      return value.toString();
    }

    function normalizeScope(value) {
      if (value === null || value === undefined) return null;
      const normalized = String(value).trim().toLowerCase();
      return VALID_SCOPES.indexOf(normalized) >= 0 ? normalized : null;
    }

    function readSavedScope() {
      try {
        return localStorage.getItem(STORAGE_SCOPE_KEY);
      } catch (_) {
        return null;
      }
    }

    function saveScopeChoice(scope) {
      try {
        localStorage.setItem(STORAGE_SCOPE_KEY, scope);
      } catch (_) {
        // Local storage can be disabled; the in-memory scope still applies.
      }
    }

    // Mirror of the backend resolve_scope() precedence, plus a convenience for
    // hand-crafted URLs that encode "all" as a present-but-empty project_path.
    function resolveInitialScope(urlHasProject, urlProject) {
      const urlScope = normalizeScope(searchParams.get("scope"));
      if (urlScope) return urlScope;
      if (urlHasProject && urlProject === "") return "all";
      const saved = normalizeScope(readSavedScope());
      if (saved) return saved;
      const envDefault = normalizeScope(LIVE_CONFIG.scope);
      if (envDefault) return envDefault;
      return DEFAULT_SCOPE;
    }

    function syncScopeUrl() {
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("scope", state.scope);
        // Keep the active project in the URL so "This project" and reloads
        // remember which project is current; in all-scope the scope param drives
        // the blend while project_path remains the identity hint.
        const identity = state.scope === "all" ? state.currentProjectPath : state.projectPath;
        if (identity) {
          url.searchParams.set("project_path", identity);
        } else {
          url.searchParams.delete("project_path");
        }
        url.searchParams.delete("dag_id");
        history.replaceState({ scope: state.scope, projectPath: state.projectPath }, "", `${url.pathname}${url.search}${url.hash}`);
      } catch (_) {
        // History updates are progressive enhancement; scope switching still works.
      }
    }

    function updateScopeControls() {
      document.querySelectorAll("#scopeToggle .view-toggle-button").forEach((button) => {
        const isActive = button.dataset.scope === state.scope;
        button.classList.toggle("active", isActive);
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
      });
      const select = $("projectSelect");
      if (!select) return;
      const target = state.scope === "all" ? (state.currentProjectPath || "") : (state.projectPath || "");
      if (Array.from(select.options).some((option) => option.value === target)) {
        select.value = target;
      }
    }

    function renderProjectOptions() {
      const select = $("projectSelect");
      if (!select) return;
      const projects = state.projects || [];
      if (!projects.length) {
        select.innerHTML = '<option value="">No projects yet</option>';
        return;
      }
      select.innerHTML = projects.map((project) => {
        const value = project.project_path || "";
        const badge = project.live ? " ●" : "";
        const label = `${project.project_name || "Global"} (${project.dag_count || 0})${badge}`;
        return `<option value="${escapeText(value)}">${escapeText(label)}</option>`;
      }).join("");
      updateScopeControls();
    }

    function fetchProjects() {
      return fetch("/api/projects", { cache: "no-store" })
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
          if (!data) return;
          state.projects = data.projects || [];
          if (typeof data.recent_window_secs === "number") state.recentWindowSecs = data.recent_window_secs;
          renderProjectOptions();
        })
        .catch(() => {
          // The projects index is best-effort; the trace view works without it.
        });
    }

    function switchScopeProject(nextScope, nextProjectPath) {
      state.scope = nextScope;
      state.projectPath = nextProjectPath;
      saveScopeChoice(nextScope);
      const token = ++state.traceToken;
      state.streamToken += 1;
      resetSnapshotRetry();
      if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
      }
      if (state.snapshotController) state.snapshotController.abort();
      const controller = new AbortController();
      state.snapshotController = controller;
      // Clear the selected trace so the new scope renders its own default trace.
      state.dagId = null;
      resetSeenNodes();
      updateScopeControls();
      syncScopeUrl();
      setBusy(true);
      setTraceSwitching(true);
      setStatus("Switching scope", "syncing");
      const applySnapshot = (snapshot) => {
        if (token !== state.traceToken) return;
        render(snapshot);
        setTraceSwitching(false);
        runTraceEnterAnimation();
        setBusy(false);
        setStatus("Connecting stream", "syncing");
        connectEvents();
      };
      fetchSnapshot(null, controller.signal)
        .then(applySnapshot)
        .catch((error) => handleSnapshotFailure(error, null, token, applySnapshot))
        .finally(() => {
          if (state.snapshotController === controller) state.snapshotController = null;
        });
    }

    function setScope(scope) {
      const next = normalizeScope(scope) || DEFAULT_SCOPE;
      if (next === "all") {
        if (state.scope === "all") return;
        switchScopeProject("all", state.currentProjectPath);
      } else {
        // "This project" reverts to the current/launch project.
        if (state.scope === "project" && state.projectPath === state.currentProjectPath) return;
        switchScopeProject("project", state.currentProjectPath);
      }
    }

    function selectProject(path) {
      const value = path || "";
      if (!value) {
        // The global / None-path project cannot be scoped to via project_path;
        // selecting it blends, the same as the all-projects view.
        setScope("all");
        return;
      }
      if (state.scope === "project" && state.projectPath === value) return;
      switchScopeProject("project", value);
    }

    function syncDagUrl(dagId) {
      try {
        const url = new URL(window.location.href);
        if (dagId) {
          url.searchParams.set("dag_id", dagId);
        } else {
          url.searchParams.delete("dag_id");
        }
        history.replaceState({ dagId }, "", `${url.pathname}${url.search}${url.hash}`);
      } catch (_) {
        // History updates are progressive enhancement; trace switching still works.
      }
    }

    function setStatus(text, mode = "idle") {
      const nextMode = mode === true ? "live" : mode === false ? "syncing" : mode;
      $("statusText").textContent = text;
      $("status").dataset.mode = nextMode;
      $("statusDot").className = `status-dot ${nextMode}`;
      $("statusSpinner").hidden = !["loading", "syncing", "reconnecting"].includes(nextMode);
    }

    function showIncomingStatus(count) {
      if (state.incomingTimer) window.clearTimeout(state.incomingTimer);
      if (count > 0) {
        setStatus(`+${count} atom${count === 1 ? "" : "s"}`, "syncing");
        state.incomingTimer = window.setTimeout(() => setStatus("Streaming", "live"), 900);
      } else {
        setStatus("Streaming", "live");
      }
    }

    function resetSeenNodes() {
      state.hasRenderedSnapshot = false;
      state.newNodeIds = new Set();
      state.seenNodeIds = new Set();
    }

    function skeletonCards(count) {
      return Array.from({ length: count }, (_, index) => `
        <div class="skeleton-card" aria-hidden="true" style="animation-delay:${index * 70}ms">
          <div class="skeleton-line medium"></div>
          <div class="skeleton-line long"></div>
          <div class="skeleton-line short"></div>
        </div>
      `).join("");
    }

    function renderGraphLoading() {
      const svg = $("graph");
      svg.setAttribute("viewBox", "0 0 420 220");
      svg.innerHTML = `
        <g opacity="0.75">
          <rect x="22" y="78" width="96" height="38" rx="2" fill="var(--graph-node-fill)" stroke="var(--border)" />
          <rect x="164" y="48" width="96" height="38" rx="2" fill="var(--graph-node-fill)" stroke="var(--border)" />
          <rect x="306" y="98" width="96" height="38" rx="2" fill="var(--graph-node-fill)" stroke="var(--border)" />
          <line x1="118" y1="97" x2="164" y2="67" stroke="var(--amber)" stroke-width="1.6" stroke-dasharray="8 8" class="graph-edge is-new" />
          <line x1="260" y1="67" x2="306" y2="117" stroke="var(--sage)" stroke-width="1.6" stroke-dasharray="8 8" class="graph-edge is-new" />
        </g>
      `;
    }

    function renderLoadingShell(label = "Loading trace stream") {
      setBusy(true);
      setStatus(label, "loading");
      const loadingCopy = `
        <div class="loading-copy">
          <span class="status-spinner" aria-hidden="true"></span>
          ${escapeText(label)}
        </div>
      `;
      $("dagList").innerHTML = `<div class="loading-stage">${loadingCopy}${skeletonCards(3)}</div>`;
      $("feed").innerHTML = `<div class="loading-stage">${loadingCopy}${skeletonCards(4)}</div>`;
      $("frontier").innerHTML = `<div class="loading-stage">${skeletonCards(2)}</div>`;
      $("astConnections").innerHTML = `<div class="loading-stage">${skeletonCards(3)}</div>`;
      $("summary").innerHTML = `<span class="loading-copy"><span class="status-spinner" aria-hidden="true"></span>Preparing summary</span>`;
      $("dagCount").textContent = "...";
      $("atomCount").textContent = "...";
      $("frontierCount").textContent = "...";
      $("edgeCount").textContent = "...";
      $("astCount").textContent = "...";
      $("updatedAt").textContent = "";
      renderGraphLoading();
    }

    function escapeText(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[char]));
    }

    async function fetchSnapshot(dagId = state.dagId, signal = undefined) {
      const query = params({ dag_id: dagId || "" });
      const response = await fetch(`/api/snapshot?${query}`, { cache: "no-store", signal });
      if (!response.ok) throw new Error(`snapshot ${response.status}`);
      return response.json();
    }

    function isNetworkFetchError(error) {
      return error && error.name === "TypeError" && /failed to fetch|networkerror|load failed/i.test(error.message || "");
    }

    function resetSnapshotRetry() {
      if (state.snapshotRetryTimer) window.clearTimeout(state.snapshotRetryTimer);
      state.snapshotRetryTimer = null;
      state.snapshotRetryDelay = 1000;
    }

    function handleSnapshotFailure(error, dagId, token, onRecover) {
      if (error.name === "AbortError" || token !== state.traceToken) return;
      if (!isNetworkFetchError(error)) {
        setTraceSwitching(false);
        setBusy(false);
        setStatus(error.message, "error");
        return;
      }
      scheduleSnapshotRetry(dagId, token, onRecover);
    }

    function scheduleSnapshotRetry(dagId, token, onRecover) {
      if (state.snapshotRetryTimer) return;
      const retryDagId = dagId || state.dagId || "";
      const delay = state.snapshotRetryDelay;
      state.snapshotRetryDelay = Math.min(5000, Math.round(delay * 1.7));
      setBusy(true);
      setStatus("Reconnecting", "reconnecting");
      state.snapshotRetryTimer = window.setTimeout(() => {
        state.snapshotRetryTimer = null;
        if (token !== state.traceToken) return;
        fetchSnapshot(retryDagId)
          .then((snapshot) => {
            if (token !== state.traceToken) return;
            resetSnapshotRetry();
            onRecover(snapshot);
          })
          .catch((error) => handleSnapshotFailure(error, retryDagId, token, onRecover));
      }, delay);
    }

    function renderTraceViewToggle(snapshot) {
      const views = (snapshot && snapshot.trace_views) || {};
      const active = views.active || "checkpoint";
      document.querySelectorAll("#viewToggle .view-toggle-button").forEach((button) => {
        const mode = button.dataset.viewMode;
        const target = views[mode];
        const isActive = mode === active;
        button.classList.toggle("active", isActive);
        button.disabled = !target || !target.dag_id || target.dag_id === state.dagId;
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
        if (target && target.dag_id) {
          button.dataset.dagId = target.dag_id;
          button.title = `${target.title} (${target.node_count || 0} atoms)`;
        } else {
          button.dataset.dagId = "";
          button.title = `${mode === "checkpoint" ? "Checkpoint" : "Exhaust"} trace unavailable`;
        }
      });
    }

    function updateDagSelection(dagId) {
      const select = $("dagSelect");
      if (select && dagId && Array.from(select.options).some((option) => option.value === dagId)) {
        select.value = dagId;
      }
      document.querySelectorAll(".dag-item").forEach((button) => {
        const isActive = button.dataset.dagId === dagId;
        button.classList.toggle("active", isActive);
        button.toggleAttribute("aria-current", isActive);
      });
    }

    function selectDag(dagId, reconnect = true) {
      if (!dagId || dagId === state.dagId) return;
      const token = ++state.traceToken;
      state.streamToken += 1;
      resetSnapshotRetry();
      if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
      }
      if (state.snapshotController) state.snapshotController.abort();
      const controller = new AbortController();
      state.snapshotController = controller;
      state.dagId = dagId;
      resetSeenNodes();
      updateDagSelection(dagId);
      syncDagUrl(dagId);
      setBusy(true);
      setTraceSwitching(true);
      setStatus("Switching trace", "syncing");
      const applySnapshot = (snapshot) => {
        if (token !== state.traceToken) return;
        render(snapshot);
        setTraceSwitching(false);
        runTraceEnterAnimation();
        setBusy(false);
        if (reconnect) {
          setStatus("Connecting stream", "syncing");
          connectEvents();
        } else {
          setStatus("Synced", "live");
        }
      };
      fetchSnapshot(dagId, controller.signal)
        .then(applySnapshot)
        .catch((error) => handleSnapshotFailure(error, dagId, token, applySnapshot))
        .finally(() => {
          if (state.snapshotController === controller) state.snapshotController = null;
        });
    }

    function render(snapshot) {
      state.snapshot = snapshot;
      if (!snapshot || !snapshot.dag) {
        $("dagList").innerHTML = '<div class="empty">No local DAGs yet.</div>';
        $("feed").innerHTML = '<div class="empty">Waiting for atoms.</div>';
        $("frontier").innerHTML = "";
        $("astConnections").innerHTML = "";
        $("summary").textContent = "";
        $("dagCount").textContent = "0";
        $("atomCount").textContent = "0";
        $("frontierCount").textContent = "0";
        $("edgeCount").textContent = "0";
        $("astCount").textContent = "0";
        $("updatedAt").textContent = "";
        renderGraph([], []);
        renderTraceViewToggle(snapshot);
        resetSeenNodes();
        return 0;
      }

      const nodes = snapshot.nodes || [];
      const frontier = snapshot.frontier || [];
      const newNodeIds = new Set();
      if (state.hasRenderedSnapshot) {
        for (const node of nodes) {
          if (node.id && !state.seenNodeIds.has(node.id)) newNodeIds.add(node.id);
        }
      }
      state.newNodeIds = newNodeIds;
      state.dagId = snapshot.dag.dag_id;
      document.title = `${snapshot.dag.title} - Scholialang Live`;
      syncDagUrl(state.dagId);
      renderDagControls(snapshot);
      renderTraceViewToggle(snapshot);
      renderAtoms(nodes, newNodeIds);
      renderFrontier(frontier, newNodeIds);
      renderGraph(nodes, snapshot.edges || [], newNodeIds);
      renderAstConnections(snapshot.ast_connections || [], newNodeIds);
      $("summary").textContent = snapshot.summary || "";
      $("atomCount").textContent = `${snapshot.dag.node_count || 0}`;
      $("frontierCount").textContent = `${frontier.length}`;
      $("edgeCount").textContent = `${snapshot.dag.edge_count || 0}`;
      $("astCount").textContent = `${(snapshot.ast_connections || []).length}`;
      $("updatedAt").textContent = snapshot.dag.updated_at || "";
      for (const node of nodes) {
        if (node.id) state.seenNodeIds.add(node.id);
      }
      state.hasRenderedSnapshot = true;
      return newNodeIds.size;
    }

    function renderDagControls(snapshot) {
      const dags = snapshot.dags || [];
      $("dagCount").textContent = `${dags.length}`;
      $("dagSelect").innerHTML = dags.map((dag) => {
        const full = dag.title || dag.dag_id;
        const pn = dag.project_name || "";
        // In single-project scope the project name is already shown in the
        // project dropdown, so strip it from the label; keep it in All-projects
        // mode for disambiguation. Drop the noisy (dag_id) suffix either way —
        // the full title + id live in the hover tooltip and the DAGs panel.
        let label = full;
        if (state.scope !== "all" && pn && label.indexOf(pn) === 0) {
          label = label.slice(pn.length).trim().replace(/^[–·:-]+/, "").trim() || full;
        }
        const selected = dag.dag_id === state.dagId ? "selected" : "";
        return `<option value="${escapeText(dag.dag_id)}" title="${escapeText(full + " (" + dag.dag_id + ")")}" ${selected}>${escapeText(label)}</option>`;
      }).join("");
      $("dagList").innerHTML = dags.map((dag) => `
        <button type="button" class="dag-item ${dag.dag_id === state.dagId ? "active" : ""}" data-dag-id="${escapeText(dag.dag_id)}"${dag.dag_id === state.dagId ? ' aria-current="true"' : ""}>
          <div class="dag-title">${escapeText(dag.title)}</div>
          <div class="dag-sub">${escapeText(dag.dag_id)}</div>
          <div class="dag-sub">${escapeText(dag.trace_view_mode || "checkpoint")}</div>
          <div class="dag-sub">${dag.node_count || 0} atoms / ${dag.edge_count || 0} edges</div>
        </button>
      `).join("");
      document.querySelectorAll(".dag-item").forEach((button) => {
        button.addEventListener("click", () => selectDag(button.dataset.dagId));
      });
    }

    function renderAtoms(nodes, newNodeIds = state.newNodeIds) {
      if (!nodes.length) {
        $("feed").innerHTML = '<div class="empty">Waiting for atoms.</div>';
        return;
      }
      const orderedNodes = state.feedOrder === "oldest" ? nodes.slice() : nodes.slice().reverse();
      $("feed").innerHTML = orderedNodes.map((node, index) => {
        const category = categoryForKind(node.kind);
        const isNew = node.id && newNodeIds.has(node.id);
        const className = `atom${isNew ? " is-new" : ""}`;
        const delay = isNew ? ` style="--enter-delay:${Math.min(index, 8) * 22}ms"` : "";
        return `
        <article class="${className}" data-node-id="${escapeText(node.id)}" data-kind="${escapeText(node.kind)}" data-cat="${category}"${delay}>
          <div>
            <div class="kind-row">
              <div class="kind">${escapeText(node.kind)}</div>
              <div class="category-chip">${categoryLabel(category)}</div>
            </div>
            <div class="atom-id">${escapeText(node.id)}</div>
            <div class="atom-id">${escapeText(node.created_at || "")}</div>
          </div>
          <div>
            <div class="atom-summary">${escapeText(node.summary)}</div>
            ${node.content ? `<div class="atom-content">${escapeText(node.content)}</div>` : ""}
            ${node.files && node.files.length ? `<div class="files">${node.files.map((file) => `<span class="file-chip">${escapeText(file)}</span>`).join("")}</div>` : ""}
          </div>
        </article>
      `;
      }).join("");
    }

    function renderFrontier(nodes, newNodeIds = state.newNodeIds) {
      $("frontier").innerHTML = nodes.length ? nodes.map((node, index) => {
        const category = categoryForKind(node.kind);
        const isNew = node.id && newNodeIds.has(node.id);
        const className = `frontier-item${isNew ? " is-new" : ""}`;
        const delay = isNew ? ` style="--enter-delay:${Math.min(index, 6) * 24}ms"` : "";
        return `
        <div class="${className}" data-node-id="${escapeText(node.id)}" data-cat="${category}"${delay}>
          <div class="kind-row">
            <div class="kind">${escapeText(node.kind)}</div>
            <div class="category-chip">${categoryLabel(category)}</div>
          </div>
          <div class="atom-summary" style="margin-top:6px">${escapeText(node.summary)}</div>
          <div class="atom-id">${escapeText(node.id)}</div>
        </div>
      `;
      }).join("") : '<div class="empty">No frontier nodes.</div>';
    }

    function renderConnectionNode(label, detail, kind) {
      const category = categoryForKind(kind);
      return `
        <div class="connection-node" data-cat="${category}">
          <div class="connection-label">${escapeText(label || "Node")}</div>
          ${detail ? `<div class="connection-detail">${escapeText(detail)}</div>` : ""}
        </div>
      `;
    }

    function connectionTouchesNewNode(connection, newNodeIds) {
      if (!newNodeIds.size) return false;
      const haystack = [
        connection.source_id,
        connection.source_detail,
        connection.target_id,
        connection.target_detail,
      ].filter(Boolean).join(" ");
      for (const id of newNodeIds) {
        if (id && haystack.includes(id)) return true;
      }
      return false;
    }

    function renderAstConnections(connections, newNodeIds = state.newNodeIds) {
      if (!connections.length) {
        $("astConnections").innerHTML = '<div class="empty">No AST connections yet.</div>';
        return;
      }
      $("astConnections").innerHTML = connections.map((connection, index) => {
        const isNew = connectionTouchesNewNode(connection, newNodeIds);
        const className = `connection-row${isNew ? " is-new" : ""}`;
        const delay = isNew ? ` style="--enter-delay:${Math.min(index, 6) * 20}ms"` : "";
        return `
        <div class="${className}" data-origin="${escapeText(connection.origin || "ast")}"${delay}>
          ${renderConnectionNode(connection.source_label, connection.source_detail || connection.source_id, connection.source_kind)}
          <div class="connection-relation">
            <span>${escapeText(connection.relation || "connects")}</span>
            <div class="connection-origin">${escapeText(connection.origin || "ast")}</div>
          </div>
          ${renderConnectionNode(connection.target_label, connection.target_detail || connection.target_id, connection.target_kind)}
        </div>
      `;
      }).join("");
    }

    function renderGraph(nodes, edges, newNodeIds = state.newNodeIds) {
      const svg = $("graph");
      const width = Math.max(320, nodes.length * 132 + 28);
      const height = 220;
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      svg.innerHTML = "";

      const ns = "http://www.w3.org/2000/svg";
      const byId = new Map();
      nodes.forEach((node, index) => {
        byId.set(node.id, { x: 24 + index * 132, y: 72 + (index % 2) * 48, node });
      });

      const defs = document.createElementNS(ns, "defs");
      CATEGORY_ORDER.forEach((category) => {
        const marker = document.createElementNS(ns, "marker");
        marker.setAttribute("id", `arrow-${category}`);
        marker.setAttribute("markerWidth", "8");
        marker.setAttribute("markerHeight", "8");
        marker.setAttribute("refX", "7");
        marker.setAttribute("refY", "4");
        marker.setAttribute("orient", "auto");
        const path = document.createElementNS(ns, "path");
        path.setAttribute("d", "M0,0 L8,4 L0,8 z");
        path.setAttribute("fill", categoryColor(category));
        marker.appendChild(path);
        defs.appendChild(marker);
      });
      svg.appendChild(defs);

      edges.forEach((edge) => {
        const from = byId.get(edge.from);
        const to = byId.get(edge.to);
        if (!from || !to) return;
        const category = categoryForKind(from.node.kind);
        const line = document.createElementNS(ns, "line");
        line.setAttribute("x1", from.x + 92);
        line.setAttribute("y1", from.y + 18);
        line.setAttribute("x2", to.x);
        line.setAttribute("y2", to.y + 18);
        line.setAttribute("stroke", categoryColor(category));
        line.setAttribute("stroke-width", "1.6");
        line.setAttribute("stroke-opacity", "0.78");
        line.setAttribute("marker-end", `url(#arrow-${category})`);
        if (newNodeIds.has(edge.from) || newNodeIds.has(edge.to)) {
          line.setAttribute("class", "graph-edge is-new");
        } else {
          line.setAttribute("class", "graph-edge");
        }
        svg.appendChild(line);
      });

      nodes.forEach((node) => {
        const point = byId.get(node.id);
        const category = categoryForKind(node.kind);
        const color = categoryColor(category);
        const group = document.createElementNS(ns, "g");
        group.setAttribute("class", `graph-node${newNodeIds.has(node.id) ? " is-new" : ""}`);
        const rect = document.createElementNS(ns, "rect");
        rect.setAttribute("x", point.x);
        rect.setAttribute("y", point.y);
        rect.setAttribute("width", "96");
        rect.setAttribute("height", "38");
        rect.setAttribute("rx", "2");
        rect.setAttribute("fill", themeColor("--graph-node-fill", "#171a15"));
        rect.setAttribute("stroke", color);
        rect.setAttribute("stroke-width", "1.8");
        group.appendChild(rect);

        const bar = document.createElementNS(ns, "rect");
        bar.setAttribute("x", point.x);
        bar.setAttribute("y", point.y);
        bar.setAttribute("width", "3");
        bar.setAttribute("height", "38");
        bar.setAttribute("rx", "1");
        bar.setAttribute("fill", color);
        group.appendChild(bar);

        const label = document.createElementNS(ns, "text");
        label.setAttribute("x", point.x + 8);
        label.setAttribute("y", point.y + 16);
        label.setAttribute("font-size", "10");
        label.setAttribute("font-weight", "700");
        label.setAttribute("fill", themeColor("--graph-text", "#f2ecde"));
        label.textContent = node.kind || "Atom";
        group.appendChild(label);

        const id = document.createElementNS(ns, "text");
        id.setAttribute("x", point.x + 8);
        id.setAttribute("y", point.y + 30);
        id.setAttribute("font-size", "9");
        id.setAttribute("fill", themeColor("--graph-muted", "#968d7e"));
        id.textContent = node.id || "";
        group.appendChild(id);
        svg.appendChild(group);
      });
    }

    function connectEvents() {
      const streamToken = ++state.streamToken;
      if (state.eventSource) state.eventSource.close();
      const streamDagId = state.dagId || "";
      const query = params({ dag_id: streamDagId });
      const isCurrentStream = () => streamToken === state.streamToken && streamDagId === (state.dagId || "");
      setStatus("Connecting stream", "syncing");
      state.eventSource = new EventSource(`/events?${query}`);
      state.eventSource.onopen = () => {
        if (!isCurrentStream()) return;
        setBusy(false);
        setStatus("Streaming", "live");
      };
      state.eventSource.addEventListener("snapshot", (event) => {
        if (!isCurrentStream()) return;
        const newCount = render(JSON.parse(event.data));
        setBusy(false);
        showIncomingStatus(newCount);
      });
      state.eventSource.onerror = () => {
        if (!isCurrentStream()) return;
        setBusy(true);
        setStatus("Reconnecting", "reconnecting");
      };
    }

    applyTheme(state.theme);

    $("dagSelect").addEventListener("change", (event) => selectDag(event.target.value));
    document.querySelectorAll("#scopeToggle .view-toggle-button").forEach((button) => {
      button.addEventListener("click", () => setScope(button.dataset.scope));
    });
    $("projectSelect").addEventListener("change", (event) => selectProject(event.target.value));
    document.querySelectorAll("#viewToggle .view-toggle-button").forEach((button) => {
      button.addEventListener("click", () => {
        if (!button.disabled && button.dataset.dagId) selectDag(button.dataset.dagId);
      });
    });
    document.querySelectorAll("#orderToggle .order-toggle-button").forEach((button) => {
      button.addEventListener("click", () => applyFeedOrder(button.dataset.feedOrder));
    });
    $("themeToggle").addEventListener("click", () => applyTheme(state.theme === "dark" ? "light" : "dark"));
    $("refresh").addEventListener("click", () => {
      const token = ++state.traceToken;
      resetSnapshotRetry();
      if (state.snapshotController) state.snapshotController.abort();
      const controller = new AbortController();
      state.snapshotController = controller;
      setBusy(true);
      setTraceSwitching(true);
      setStatus("Syncing", "syncing");
      const applySnapshot = (snapshot) => {
        if (token !== state.traceToken) return;
        const newCount = render(snapshot);
        setTraceSwitching(false);
        runTraceEnterAnimation();
        setBusy(false);
        showIncomingStatus(newCount);
      };
      fetchSnapshot(state.dagId, controller.signal)
        .then(applySnapshot)
        .catch((error) => handleSnapshotFailure(error, state.dagId, token, applySnapshot))
        .finally(() => {
          if (state.snapshotController === controller) state.snapshotController = null;
        });
    });

    applyFeedOrder(state.feedOrder);
    updateScopeControls();
    fetchProjects();
    state.projectPollTimer = window.setInterval(fetchProjects, PROJECT_POLL_MS);
    renderLoadingShell();
    const initialToken = state.traceToken;
    const applyInitialSnapshot = (snapshot) => {
      if (initialToken !== state.traceToken) return;
      resetSnapshotRetry();
      render(snapshot);
      setStatus("Connecting stream", "syncing");
      connectEvents();
    };
    fetchSnapshot()
      .then(applyInitialSnapshot)
      .catch((error) => handleSnapshotFailure(error, state.dagId, initialToken, applyInitialSnapshot));
  </script>
  <style>
    .scholia-settings__fab {
      position: fixed; right: 18px; bottom: 18px; z-index: 9999;
      width: 40px; height: 40px; border-radius: 999px; cursor: pointer;
      border: 1px solid var(--border, rgba(255, 255, 255, 0.16));
      background: var(--bg-2, #131611); color: var(--fg, #e8e8e3);
      font-size: 18px; line-height: 1; box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
    }
    .scholia-settings { position: fixed; right: 18px; bottom: 68px; z-index: 9999; }
    .scholia-settings__panel {
      width: 320px; max-width: calc(100vw - 36px); padding: 14px 14px 12px;
      border-radius: 12px; border: 1px solid var(--border, rgba(255, 255, 255, 0.16));
      background: var(--bg-2, #131611); color: var(--fg, #e8e8e3);
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45); font-size: 13px;
    }
    .scholia-settings__head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
    .scholia-settings__x { background: none; border: 0; color: inherit; font-size: 18px; cursor: pointer; line-height: 1; }
    .scholia-settings__row { display: flex; align-items: center; gap: 8px; cursor: pointer; }
    .scholia-settings__note { margin: 6px 0 10px; opacity: 0.7; font-size: 12px; }
    .scholia-settings__meta { margin: 0; display: grid; gap: 6px; }
    .scholia-settings__meta div { display: grid; grid-template-columns: 92px 1fr; gap: 8px; }
    .scholia-settings__meta dt { opacity: 0.6; }
    .scholia-settings__meta dd { margin: 0; word-break: break-all; opacity: 0.92; }
  </style>
  <button type="button" id="scholiaSettingsToggle" class="scholia-settings__fab" aria-label="Settings" title="Scholia settings">&#9881;</button>
  <div id="scholiaSettings" class="scholia-settings" hidden>
    <div class="scholia-settings__panel" role="dialog" aria-label="Scholia settings">
      <div class="scholia-settings__head"><strong>Scholia settings</strong><button type="button" id="scholiaSettingsClose" class="scholia-settings__x" aria-label="Close">&times;</button></div>
      <label class="scholia-settings__row"><input type="checkbox" id="scholiaAutoEmit"><span>Auto-emit for this project</span></label>
      <p id="scholiaAutoEmitNote" class="scholia-settings__note"></p>
      <dl class="scholia-settings__meta">
        <div><dt>Project</dt><dd id="scholiaMetaProject">&mdash;</dd></div>
        <div><dt>Live mode</dt><dd id="scholiaMetaLive">&mdash;</dd></div>
        <div><dt>Storage</dt><dd id="scholiaMetaHome">&mdash;</dd></div>
        <div><dt>Database</dt><dd id="scholiaMetaDb">&mdash;</dd></div>
      </dl>
    </div>
  </div>
  <script>
    (function () {
      const qp = new URLSearchParams(location.search);
      const project = qp.get("project_path") || "";
      const fab = document.getElementById("scholiaSettingsToggle");
      const panel = document.getElementById("scholiaSettings");
      const closeBtn = document.getElementById("scholiaSettingsClose");
      const checkbox = document.getElementById("scholiaAutoEmit");
      const note = document.getElementById("scholiaAutoEmitNote");
      const setText = function (id, v) { const el = document.getElementById(id); if (el) el.textContent = v; };
      function render(s) {
        s = s || {};
        checkbox.checked = !!s.auto_emit;
        checkbox.disabled = !!s.env_autoemit_disabled || !s.project_path;
        note.textContent = s.env_autoemit_disabled
          ? "Forced off by SCHOLIA_AUTOEMIT=" + (s.env_autoemit || "") + " (env overrides this toggle)."
          : (s.marker_exists ? "Off — .scholia-off present in project root." : "On — no .scholia-off marker.");
        setText("scholiaMetaProject", s.project_path || "—");
        setText("scholiaMetaLive", s.live_enabled ? "enabled (SCHOLIA_LIVE)" : "disabled");
        setText("scholiaMetaHome", s.storage_home || "—");
        setText("scholiaMetaDb", s.database_path || "—");
      }
      function load() {
        fetch("/api/settings?project_path=" + encodeURIComponent(project), { cache: "no-store" })
          .then(function (r) { return r.json(); }).then(render).catch(function () {});
      }
      checkbox.addEventListener("change", function () {
        fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_path: project, auto_emit: checkbox.checked }),
        }).then(function (r) { return r.json(); }).then(render).catch(load);
      });
      fab.addEventListener("click", function () { panel.hidden = !panel.hidden; if (!panel.hidden) load(); });
      closeBtn.addEventListener("click", function () { panel.hidden = true; });
      load();
    }());
  </script>
</body>
</html>
"""


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def query_one(query, key, default=None):
    values = query.get(key)
    if not values:
        return default
    return values[0]


def ast_connection(
    source_id,
    source_label,
    source_kind,
    relation,
    target_id,
    target_label,
    target_kind,
    origin,
    source_detail="",
    target_detail="",
):
    return {
        "source_id": str(source_id or ""),
        "source_label": str(source_label or ""),
        "source_kind": str(source_kind or "Reference"),
        "source_detail": str(source_detail or ""),
        "relation": str(relation or "connects"),
        "target_id": str(target_id or ""),
        "target_label": str(target_label or ""),
        "target_kind": str(target_kind or "Reference"),
        "target_detail": str(target_detail or ""),
        "origin": str(origin or "ast"),
    }


def ast_node_id(origin_atom_id, element, ordinal):
    explicit_id = element.attrib.get("id")
    if explicit_id:
        return explicit_id
    return f"{origin_atom_id}::{element.tag}_{ordinal:03d}"


def srml_candidate(text):
    if not text or "<" not in text or ">" not in text:
        return None
    tags = list(getattr(scholia, "ATOM_KINDS", [])) + ["Trace", "Step"]
    if not any(re.search(rf"<\s*/?\s*{re.escape(tag)}\b", text) for tag in tags):
        return None
    stripped = text.strip()
    if re.match(r"^<\s*Trace\b", stripped):
        return stripped
    return f"<Trace>{stripped}</Trace>"


def walk_srml_ast(element, parent_ref, origin_atom_id, rows, counter):
    for child in list(element):
        counter[0] += 1
        child_id = ast_node_id(origin_atom_id, child, counter[0])
        child_ref = {
            "id": child_id,
            "label": f"<{child.tag}>",
            "kind": child.tag,
            "detail": child.attrib.get("id", child_id),
        }
        rows.append(
            ast_connection(
                parent_ref["id"],
                parent_ref["label"],
                parent_ref["kind"],
                "contains",
                child_ref["id"],
                child_ref["label"],
                child_ref["kind"],
                "ast",
                parent_ref.get("detail", ""),
                child_ref.get("detail", ""),
            )
        )
        for attr_name in ("for", "target", "ref", "from", "to", "source"):
            value = child.attrib.get(attr_name)
            if value:
                rows.append(
                    ast_connection(
                        child_ref["id"],
                        child_ref["label"],
                        child_ref["kind"],
                        f"attr:{attr_name}",
                        value,
                        value,
                        "Reference",
                        "ast",
                        child_ref.get("detail", ""),
                        value,
                    )
                )
        for op, target in INLINE_REF_RE.findall("".join(child.itertext())):
            rows.append(
                ast_connection(
                    child_ref["id"],
                    child_ref["label"],
                    child_ref["kind"],
                    op,
                    target,
                    target,
                    "Reference",
                    "ast",
                    child_ref.get("detail", ""),
                    target,
                )
            )
        walk_srml_ast(child, child_ref, origin_atom_id, rows, counter)


def srml_ast_connections_for_node(node):
    text = "\n".join(str(part or "") for part in (node.get("summary"), node.get("content")))
    rows = []
    for op, target in INLINE_REF_RE.findall(text):
        rows.append(
            ast_connection(
                node.get("id"),
                f"<{node.get('kind', 'Atom')}>",
                node.get("kind", "Reference"),
                op,
                target,
                target,
                "Reference",
                "inline",
                node.get("id"),
                target,
            )
        )

    candidate = srml_candidate(text)
    if not candidate:
        return rows
    try:
        root = ET.fromstring(candidate)
    except ET.ParseError:
        return rows

    origin_ref = {
        "id": node.get("id", ""),
        "label": f"<{node.get('kind', 'Atom')}>",
        "kind": node.get("kind", "Reference"),
        "detail": node.get("id", ""),
    }
    walk_srml_ast(root, origin_ref, node.get("id", "atom"), rows, [0])
    return rows


def build_ast_connections(dag, nodes, edges, limit=AST_CONNECTION_LIMIT):
    rows = []
    if not dag:
        return rows
    dag_id = dag.get("dag_id", "Trace")
    for node in nodes:
        rows.append(
            ast_connection(
                dag_id,
                "Trace",
                "Meta",
                "contains",
                node.get("id"),
                f"<{node.get('kind', 'Atom')}>",
                node.get("kind", "Reference"),
                "trace",
                dag_id,
                node.get("id"),
            )
        )

    node_by_id = {node.get("id"): node for node in nodes}
    for edge in edges:
        source = node_by_id.get(edge.get("from"), {"kind": "Reference"})
        target = node_by_id.get(edge.get("to"), {"kind": "Reference"})
        rows.append(
            ast_connection(
                edge.get("from"),
                f"<{source.get('kind', 'Atom')}>",
                source.get("kind", "Reference"),
                edge.get("relation", "links"),
                edge.get("to"),
                f"<{target.get('kind', 'Atom')}>",
                target.get("kind", "Reference"),
                "edge",
                edge.get("from"),
                edge.get("to"),
            )
        )

    for node in nodes:
        rows.extend(srml_ast_connections_for_node(node))
    return rows[:limit]


def trace_view_mode(dag):
    tags = {str(tag).lower() for tag in dag.get("tags", [])}
    title = str(dag.get("title") or "").lower()
    objective = str(dag.get("objective") or "").lower()
    if {"exhaust", "event-source"} & tags or title.startswith("codex exhaust:") or "rollout exhaust" in objective:
        return "exhaust"
    return "checkpoint"


def trace_title_key(title):
    value = str(title or "").lower()
    value = re.sub(r"^\s*codex\s+exhaust\s*:\s*", "", value)
    value = re.sub(r"\b(and|with)\s+pr\s+merge\b", "", value)
    value = re.sub(r"\b(pr|pull request)\s+merge\b", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def trace_match_score(source, candidate):
    source_key = trace_title_key(source.get("title"))
    candidate_key = trace_title_key(candidate.get("title"))
    if not source_key or not candidate_key:
        return 0
    if source_key == candidate_key:
        score = 100
    elif source_key in candidate_key or candidate_key in source_key:
        score = 82
    else:
        source_tokens = set(source_key.split())
        candidate_tokens = set(candidate_key.split())
        if not source_tokens or not candidate_tokens:
            return 0
        overlap = len(source_tokens & candidate_tokens) / max(len(source_tokens), len(candidate_tokens))
        score = int(overlap * 70)
    if score < 42:
        return 0
    return score + min(int(candidate.get("node_count") or 0), 1000) / 1000


def related_trace_views(selected_meta, dags):
    if not selected_meta:
        return {"active": "checkpoint", "checkpoint": None, "exhaust": None}
    active = selected_meta.get("trace_view_mode") or trace_view_mode(selected_meta)
    views = {"active": active, "checkpoint": None, "exhaust": None}
    views[active] = selected_meta
    target_mode = "exhaust" if active == "checkpoint" else "checkpoint"
    candidates = [
        dag
        for dag in dags
        if dag.get("dag_id") != selected_meta.get("dag_id") and (dag.get("trace_view_mode") or trace_view_mode(dag)) == target_mode
    ]
    ranked = sorted(
        ((trace_match_score(selected_meta, candidate), candidate) for candidate in candidates),
        key=lambda item: (item[0], str(item[1].get("updated_at") or "")),
        reverse=True,
    )
    if ranked and ranked[0][0] > 0:
        views[target_mode] = ranked[0][1]
    return views


def enrich_dag_metadata(dag):
    meta = scholia.dag_metadata(dag)
    meta["trace_view_mode"] = trace_view_mode(meta)
    return meta


def default_trace_dag_id(dags):
    for dag in dags:
        if (dag.get("trace_view_mode") or trace_view_mode(dag)) != "checkpoint":
            continue
        views = related_trace_views(dag, dags)
        if views.get("checkpoint") and views.get("exhaust"):
            return dag.get("dag_id")
    return dags[0].get("dag_id") if dags else None


def load_snapshot(dag_id=None, project_path=None, limit=80):
    dags = [enrich_dag_metadata(dag) for dag in scholia.all_dags(project_path)]
    selected = None
    if dag_id:
        selected = scholia.load_dag(dag_id, project_path)
    elif dags:
        selected = scholia.load_dag(default_trace_dag_id(dags), project_path)

    if selected is None:
        return {
            "database_path": str(scholia.database_path()),
            "generated_at": scholia.now(),
            "project_path": project_path,
            "dags": dags,
            "dag": None,
            "nodes": [],
            "edges": [],
            "frontier": [],
            "ast_connections": [],
            "trace_views": {"active": "checkpoint", "checkpoint": None, "exhaust": None},
            "summary": "",
        }

    node_ids = selected.get("order", [])[-limit:]
    node_set = set(node_ids)
    nodes = [selected["nodes"][node_id] for node_id in node_ids if node_id in selected["nodes"]]
    edges = [
        edge
        for edge in selected.get("edges", [])
        if edge.get("from") in node_set and edge.get("to") in node_set
    ][-limit * 3 :]
    selected_meta = enrich_dag_metadata(selected)
    return {
        "database_path": str(scholia.database_path()),
        "generated_at": scholia.now(),
        "project_path": project_path,
        "dags": dags,
        "dag": selected_meta,
        "nodes": nodes,
        "edges": edges,
        "frontier": scholia.frontier_nodes(selected)[:20],
        "ast_connections": build_ast_connections(selected, nodes, edges),
        "trace_views": related_trace_views(selected_meta, dags),
        "summary": scholia.build_summary(selected, max_items=10),
    }


def snapshot_fingerprint(snapshot):
    dag = snapshot.get("dag") or {}
    return (
        dag.get("dag_id"),
        dag.get("updated_at"),
        dag.get("node_count"),
        dag.get("edge_count"),
        len(snapshot.get("dags", [])),
    )


AUTO_EMIT_MARKER = ".scholia-off"
AUTOEMIT_OFF_VALUES = {"0", "false", "off", "no"}
LIVE_ON_VALUES = {"1", "true", "on", "yes"}


def _auto_emit_marker(project_path):
    if not project_path:
        return None
    return Path(project_path).expanduser() / AUTO_EMIT_MARKER


def _env_autoemit_disabled():
    flag = os.environ.get("SCHOLIA_AUTOEMIT")
    return flag is not None and flag.strip().lower() in AUTOEMIT_OFF_VALUES


def _env_live_enabled():
    flag = os.environ.get("SCHOLIA_LIVE")
    return flag is not None and flag.strip().lower() in LIVE_ON_VALUES


def read_settings_state(project_path):
    marker = _auto_emit_marker(project_path)
    marker_exists = bool(marker and marker.exists())
    env_off = _env_autoemit_disabled()
    storage_home = Path(os.environ.get("SCHOLIALANG_HOME") or "~/.scholialang").expanduser()
    return {
        "project_path": project_path or "",
        "auto_emit": (not env_off) and (not marker_exists),
        "marker_exists": marker_exists,
        "marker_path": str(marker) if marker else "",
        "env_autoemit_disabled": env_off,
        "env_autoemit": os.environ.get("SCHOLIA_AUTOEMIT"),
        "live_enabled": _env_live_enabled(),
        "storage_home": str(storage_home),
        "database_path": str(scholia.database_path()),
    }


def set_auto_emit(project_path, enabled):
    marker = _auto_emit_marker(project_path)
    if marker is None:
        raise ValueError("project_path is required")
    if not marker.parent.is_dir():
        raise ValueError("project_path is not a directory")
    if enabled:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
    else:
        marker.write_text("")
    return read_settings_state(project_path)


# --- Multi-project scope + projects index -------------------------------------

DEFAULT_SCOPE = "project"
VALID_SCOPES = ("project", "all")
DEFAULT_RECENT_SECS = 300


def resolve_project_path(query, default):
    """Resolve the effective project_path scope from a parsed query dict.

    Three cases, so /api/snapshot and /events follow the selected scope:
      * key absent             -> the server's launch-dir default (unchanged)
      * key present but empty   -> None (blended all-projects view)
      * key present, non-empty  -> that single project path

    Requires the query dict to preserve blank values
    (parse_qs(..., keep_blank_values=True)); otherwise an empty param is dropped
    and indistinguishable from an absent one.
    """
    if "project_path" not in query:
        return default
    values = query.get("project_path") or [""]
    return values[0] or None


def _normalize_scope(value):
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized if normalized in VALID_SCOPES else None


def resolve_scope(url_scope=None, saved_scope=None, env_scope=None):
    """Resolve the effective viewer scope by precedence.

    URL ?scope= wins, then the saved UI choice (localStorage, supplied by the
    frontend), then the SCHOLIA_LIVE_SCOPE env default, then the built-in
    'project' default. Unrecognized values are ignored at their tier.
    """
    for candidate in (url_scope, saved_scope, env_scope):
        normalized = _normalize_scope(candidate)
        if normalized:
            return normalized
    return DEFAULT_SCOPE


def env_default_scope():
    return _normalize_scope(os.environ.get("SCHOLIA_LIVE_SCOPE")) or DEFAULT_SCOPE


def recent_window_secs():
    window = parse_int(os.environ.get("SCHOLIA_LIVE_RECENT_SECS"), DEFAULT_RECENT_SECS)
    return window if window >= 0 else DEFAULT_RECENT_SECS


def live_defaults():
    """Server-side defaults the frontend reads to apply scope precedence."""
    return {
        "scope": env_default_scope(),
        "recent_window_secs": recent_window_secs(),
    }


def _parse_iso8601(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_recent(updated_at, now_dt, window_secs):
    if now_dt is None:
        return False
    updated_dt = _parse_iso8601(updated_at)
    if updated_dt is None:
        return False
    return (now_dt - updated_dt).total_seconds() <= window_secs


def build_projects(dags, now, window_secs):
    """Group cross-project DAGs into a projects index with a live flag.

    `dags` is scholia.all_dags(None) output (every project's DAGs). `now` is an
    ISO-8601 UTC timestamp (scholia.now()); `window_secs` is the recency window.
    Returns a list of {project_key, project_name, project_path, dag_count,
    last_updated, live} sorted by last_updated descending. `live` is True when the
    project's newest *session* DAG (session_key set) updated within the window;
    only session DAGs count toward liveness. The global/None-path project is
    labeled 'Global'.
    """
    now_dt = _parse_iso8601(now)
    groups = {}
    order = []
    for dag in dags:
        key = dag.get("project_key")
        if key not in groups:
            path = dag.get("project_path")
            name = dag.get("project_name") or ("Global" if not path else path)
            groups[key] = {
                "project_key": key,
                "project_name": name or "Global",
                "project_path": path,
                "dag_count": 0,
                "last_updated": None,
                "_newest_session": None,
            }
            order.append(key)
        bucket = groups[key]
        bucket["dag_count"] += 1
        updated = dag.get("updated_at")
        if updated and (bucket["last_updated"] is None or updated > bucket["last_updated"]):
            bucket["last_updated"] = updated
        if dag.get("session_key") and updated and (
            bucket["_newest_session"] is None or updated > bucket["_newest_session"]
        ):
            bucket["_newest_session"] = updated
    projects = []
    for key in order:
        bucket = groups[key]
        newest_session = bucket.pop("_newest_session")
        bucket["live"] = _is_recent(newest_session, now_dt, window_secs)
        projects.append(bucket)
    projects.sort(key=lambda item: (item["last_updated"] or ""), reverse=True)
    return projects


def render_webview_html():
    """Serve the webview HTML with server-side scope defaults injected."""
    return WEBVIEW_HTML.replace(
        "__SCHOLIA_LIVE_CONFIG__",
        json.dumps(live_defaults(), separators=(",", ":"), sort_keys=True),
    )


class ScholialangWebviewHandler(BaseHTTPRequestHandler):
    server_version = "ScholialangWebview/0.1"

    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except ConnectionResetError:
            self.close_connection = True

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if parsed.path == "/":
                self.send_html(render_webview_html())
            elif parsed.path == "/health":
                self.send_json(
                    {
                        "ok": True,
                        "database_path": str(scholia.database_path()),
                        "project_path": self.project_path(query),
                    }
                )
            elif parsed.path == "/api/dags":
                project_path = self.project_path(query)
                limit = parse_int(query_one(query, "limit"), 20)
                dags = [enrich_dag_metadata(dag) for dag in scholia.all_dags(project_path)[:limit]]
                self.send_json({"database_path": str(scholia.database_path()), "project_path": project_path, "dags": dags})
            elif parsed.path == "/api/projects":
                window = parse_int(query_one(query, "recent_secs"), recent_window_secs())
                projects = build_projects(scholia.all_dags(None), scholia.now(), window)
                self.send_json(
                    {
                        "projects": projects,
                        "recent_window_secs": window,
                        "database_path": str(scholia.database_path()),
                    }
                )
            elif parsed.path == "/api/snapshot":
                self.send_json(self.snapshot(query))
            elif parsed.path == "/api/settings":
                self.send_json(read_settings_state(self.project_path(query)))
            elif parsed.path == "/events":
                self.send_events(query)
            else:
                self.send_error_json(404, "not found")
        except ValueError as exc:
            self.send_error_json(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/settings":
                payload = self.read_json_body()
                project_path = payload.get("project_path") or getattr(self.server, "project_path", None)
                if "auto_emit" not in payload:
                    raise ValueError("auto_emit is required")
                self.send_json(set_auto_emit(project_path, bool(payload.get("auto_emit"))))
            else:
                self.send_error_json(404, "not found")
        except ValueError as exc:
            self.send_error_json(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("invalid JSON body")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def project_path(self, query):
        return resolve_project_path(query, getattr(self.server, "project_path", None))

    def snapshot(self, query):
        return load_snapshot(
            dag_id=query_one(query, "dag_id"),
            project_path=self.project_path(query),
            limit=parse_int(query_one(query, "limit"), 80),
        )

    def send_html(self, html_text):
        body = html_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"ok": False, "error": message}, status=status)

    def send_events(self, query):
        interval = max(0.1, parse_float(query_one(query, "interval"), getattr(self.server, "poll_interval", DEFAULT_POLL_SECONDS)))
        once = query_one(query, "once") == "1"
        started_at = time.monotonic()
        last_fingerprint = None

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close" if once else "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        while time.monotonic() - started_at < MAX_STREAM_SECONDS:
            snapshot = self.snapshot(query)
            fingerprint = snapshot_fingerprint(snapshot)
            if fingerprint != last_fingerprint or once:
                last_fingerprint = fingerprint
                self.write_event("snapshot", snapshot)
                if once:
                    break
            else:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            time.sleep(interval)
        if once:
            self.close_connection = True

    def write_event(self, event, payload):
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        for line in body.splitlines() or [""]:
            self.wfile.write(f"data: {line}\n".encode("utf-8"))
        self.wfile.write(b"\n")
        self.wfile.flush()


def create_server(host=DEFAULT_HOST, port=DEFAULT_PORT, project_path=None, poll_interval=DEFAULT_POLL_SECONDS, quiet=False):
    server = ThreadingHTTPServer((host, port), ScholialangWebviewHandler)
    server.project_path = project_path
    server.poll_interval = poll_interval
    server.quiet = quiet
    return server


def local_tailscale_ip():
    try:
        completed = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            check=False,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in completed.stdout.splitlines():
        candidate = line.strip()
        if re.match(r"^100\.\d{1,3}\.\d{1,3}\.\d{1,3}$", candidate):
            return candidate
    return None


def webview_url(host, port, query):
    url = f"http://{host}:{port}/"
    if query:
        url += "?" + urlencode(query)
    return url


def chrome_url(url):
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)
    elif os.name == "nt":
        subprocess.run(["cmd", "/c", "start", "chrome", url], check=False)
    else:
        subprocess.run(["google-chrome", url], check=False)


def run_server(args):
    server = create_server(args.host, args.port, args.project_path, args.poll_interval, args.quiet)
    host, port = server.server_address
    query = {}
    if args.project_path:
        query["project_path"] = args.project_path
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = webview_url(display_host, port, query)
    if args.open_chrome:
        chrome_url(url)
    print(f"Scholialang live webview: {url}", flush=True)
    tailscale_ip = local_tailscale_ip() if host in ("0.0.0.0", "::") or host.startswith("100.") else None
    if tailscale_ip:
        print(f"Scholialang phone/Tailscale webview: {webview_url(tailscale_ip, port, query)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve a local live Scholialang DAG webview.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project-path", default=None)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--open-chrome", action="store_true", help="Open the webview in Google Chrome.")
    parser.add_argument("--quiet", action="store_true", help="Suppress request logs.")
    args = parser.parse_args(argv)
    run_server(args)


if __name__ == "__main__":
    main()
