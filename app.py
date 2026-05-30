# QA Monitor - Surveillance 24/7 de sites web
# Détecte: pages down, erreurs JS, régressions visuelles, liens cassés, lenteurs
# Business model: 49€/mois par site surveillé

import os, json, time, sqlite3, hashlib
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file
from playwright.sync_api import sync_playwright
import threading

app = Flask(__name__)
DB = os.path.join(os.path.dirname(__file__), "data", "monitor.db")
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "data", "reports")
SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "data", "screenshots")

for d in [REPORTS_DIR, SCREENSHOTS_DIR]:
    os.makedirs(d, exist_ok=True)

# ---------------------------------------------------------------------------
# DB Setup
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            name TEXT,
            status TEXT DEFAULT 'ok',
            last_check TIMESTAMP,
            consecutive_failures INTEGER DEFAULT 0,
            plan TEXT DEFAULT 'free',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            status TEXT,
            response_time_ms INTEGER,
            js_errors TEXT,
            broken_links TEXT,
            screenshot_path TEXT,
            diff_score REAL,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(site_id) REFERENCES sites(id)
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------------------------
# Site Checker Engine
# ---------------------------------------------------------------------------
def check_site(site_id, url):
    results = {
        "status": "ok",
        "response_time_ms": 0,
        "js_errors": [],
        "broken_links": [],
        "screenshot_path": "",
        "diff_score": 0,
        "issues": []
    }
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        
        js_logs = []
        page.on("console", lambda msg: js_logs.append(f"[{msg.type}] {msg.text}") if msg.type in ("error", "warning") else None)
        page.on("pageerror", lambda err: js_logs.append(f"[JS ERROR] {err.message}"))
        
        try:
            start = time.time()
            response = page.goto(url, wait_until="networkidle", timeout=30000)
            results["response_time_ms"] = int((time.time() - start) * 1000)
            
            if response and response.status >= 400:
                results["status"] = "error"
                results["issues"].append(f"HTTP {response.status}")
            
            # Check for JS errors
            results["js_errors"] = js_logs
            if js_logs:
                results["issues"].append(f"{len(js_logs)} erreurs JS")
                if results["status"] == "ok":
                    results["status"] = "warning"
            
            # Check broken links
            links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            for link in links[:20]:  # Limit to first 20
                try:
                    r = page.request.head(link, timeout=5000)
                    if r.status >= 400:
                        results["broken_links"].append(link)
                except:
                    results["broken_links"].append(f"{link} (timeout)")
            
            if results["broken_links"]:
                results["issues"].append(f"{len(results['broken_links'])} liens cassés")
            
            # Screenshot
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SCREENSHOTS_DIR, f"site_{site_id}_{ts}.png")
            page.screenshot(path=path, full_page=False)
            results["screenshot_path"] = path
            
            # Compare with previous screenshot for visual regression
            prev = get_previous_screenshot(site_id)
            if prev and os.path.exists(prev):
                from PIL import Image
                try:
                    img1 = Image.open(prev).convert("RGB")
                    img2 = Image.open(path).convert("RGB")
                    # Simple pixel diff
                    diff = sum(abs(a - b) for a, b in zip(img1.resize((100, 100)).getdata(), img2.resize((100, 100)).getdata()))
                    results["diff_score"] = round(diff / (100 * 100 * 3 * 255) * 100, 2)
                    if results["diff_score"] > 10:
                        results["issues"].append(f"Diff visuelle {results['diff_score']}%")
                        if results["status"] == "ok":
                            results["status"] = "warning"
                except:
                    pass
            
        except Exception as e:
            results["status"] = "error"
            results["issues"].append(str(e)[:100])
        
        finally:
            browser.close()
    
    return results

def get_previous_screenshot(site_id):
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT screenshot_path FROM checks WHERE site_id=? ORDER BY checked_at DESC LIMIT 1",
        (site_id,)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None

def save_check(site_id, results):
    conn = sqlite3.connect(DB)
    conn.execute(
        """INSERT INTO checks (site_id, status, response_time_ms, js_errors, broken_links, screenshot_path, diff_score)
           VALUES (?,?,?,?,?,?,?)""",
        (
            site_id, results["status"], results["response_time_ms"],
            json.dumps(results["js_errors"]), json.dumps(results["broken_links"]),
            results["screenshot_path"], results["diff_score"]
        )
    )
    conn.execute(
        "UPDATE sites SET status=?, last_check=CURRENT_TIMESTAMP, consecutive_failures=? WHERE id=?",
        (results["status"], 0 if results["status"] == "ok" else None, site_id) if results["status"] != "error"
        else (results["status"], None, None, site_id)
    )
    # Handle consecutive_failures properly
    if results["status"] == "error":
        prev = conn.execute("SELECT consecutive_failures FROM sites WHERE id=?", (site_id,)).fetchone()
        if prev:
            conn.execute("UPDATE sites SET status='error', last_check=CURRENT_TIMESTAMP, consecutive_failures=? WHERE id=?",
                        (prev[0] + 1, site_id))
    else:
        conn.execute("UPDATE sites SET status=?, last_check=CURRENT_TIMESTAMP, consecutive_failures=0 WHERE id=?",
                    (results["status"], site_id))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/dashboard")
def dashboard():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sites = conn.execute("SELECT * FROM sites ORDER BY created_at DESC").fetchall()
    
    # Get recent checks
    site_data = []
    for s in sites:
        checks = conn.execute(
            "SELECT * FROM checks WHERE site_id=? ORDER BY checked_at DESC LIMIT 5",
            (s["id"],)
        ).fetchall()
        site_data.append({"site": dict(s), "checks": [dict(c) for c in checks]})
    
    conn.close()
    return render_template("dashboard.html", site_data=site_data, now=datetime.now())

@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json()
    url = data.get("url", "").strip()
    name = data.get("name", "").strip()
    
    if not url:
        return jsonify({"error": "URL required"}), 400
    
    if not url.startswith("http"):
        url = "https://" + url
    
    # Check if already exists
    conn = sqlite3.connect(DB)
    existing = conn.execute("SELECT id FROM sites WHERE url=?", (url,)).fetchone()
    
    if existing:
        site_id = existing[0]
    else:
        conn.execute("INSERT INTO sites (url, name) VALUES (?,?)", (url, name or url))
        site_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    
    conn.commit()
    conn.close()
    
    # Run check
    results = check_site(site_id, url)
    save_check(site_id, results)
    
    return jsonify({"site_id": site_id, **results})

@app.route("/api/sites")
def api_sites():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sites = conn.execute("SELECT * FROM sites ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([dict(s) for s in sites])

@app.route("/api/check/<int:site_id>")
def api_check_single(site_id):
    conn = sqlite3.connect(DB)
    site = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    
    results = check_site(site_id, site[1])
    save_check(site_id, results)
    conn.close()
    return jsonify(results)

@app.route("/api/report/<int:site_id>")
def api_report(site_id):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    site = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    checks = conn.execute(
        "SELECT * FROM checks WHERE site_id=? ORDER BY checked_at DESC LIMIT 50",
        (site_id,)
    ).fetchall()
    conn.close()
    
    if not site:
        return jsonify({"error": "Not found"}), 404
    
    # Build report
    ok_count = sum(1 for c in checks if c["status"] == "ok")
    warn_count = sum(1 for c in checks if c["status"] == "warning")
    err_count = sum(1 for c in checks if c["status"] == "error")
    
    report = {
        "site": dict(site),
        "uptime": round(ok_count / max(len(checks), 1) * 100, 1),
        "checks": [dict(c) for c in checks],
        "summary": {
            "total": len(checks),
            "ok": ok_count,
            "warnings": warn_count,
            "errors": err_count,
            "avg_response_ms": round(sum(c["response_time_ms"] for c in checks if c["response_time_ms"]) / max(len(checks), 1))
        }
    }
    return jsonify(report)

@app.route("/api/outreach", methods=["POST"])
def api_outreach():
    """Generate a free audit for outreach"""
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url or not url.startswith("http"):
        return jsonify({"error": "URL required"}), 400
    
    # Quick check
    results = check_site(0, url)
    
    # Generate audit text
    issues_found = len(results.get("issues", []))
    audit = {
        "url": url,
        "status": results["status"],
        "issues": results["issues"],
        "response_time_ms": results["response_time_ms"],
        "timestamp": datetime.now().isoformat(),
        "message": f"Audit gratuit de {url}: {issues_found} problème(s) détecté(s). Score de santé: {'PASS' if results['status'] == 'ok' else 'WARN' if results['status'] == 'warning' else 'FAIL'}"
    }
    return jsonify(audit)

# ---------------------------------------------------------------------------
# Scheduled checker (called by cron)
# ---------------------------------------------------------------------------
def check_all_sites():
    conn = sqlite3.connect(DB)
    sites = conn.execute("SELECT id, url FROM sites").fetchall()
    conn.close()
    
    results = []
    for site_id, url in sites:
        try:
            r = check_site(site_id, url)
            save_check(site_id, r)
            results.append({"site_id": site_id, "url": url, **r})
        except Exception as e:
            results.append({"site_id": site_id, "url": url, "status": "error", "error": str(e)})
    
    return results

@app.route("/api/run-all", methods=["POST"])
def api_run_all():
    results = check_all_sites()
    return jsonify({"checked": len(results), "results": results})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5051, debug=False)
