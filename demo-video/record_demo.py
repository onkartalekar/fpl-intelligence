from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8878/dashboard-demo.html"
VIDEO_DIR = ROOT / "raw-video"
VIDEO_DIR.mkdir(exist_ok=True)


def pause(page, seconds):
    page.wait_for_timeout(int(seconds * 1000))


def caption(page, title, body):
    page.evaluate(
        """([title, body]) => {
          const box = document.getElementById('demo-caption');
          box.querySelector('strong').textContent = title;
          box.querySelector('span').textContent = body;
          box.style.opacity = '1';
        }""",
        [title, body],
    )


def smooth_to(page, selector, offset=-85):
    page.evaluate(
        """([selector, offset]) => {
          const el = document.querySelector(selector);
          if (!el) return;
          const top = el.getBoundingClientRect().top + window.scrollY + offset;
          window.scrollTo({top, behavior: 'smooth'});
        }""",
        [selector, offset],
    )
    pause(page, 1.3)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1280, "height": 720},
        device_scale_factor=1,
        record_video_dir=str(VIDEO_DIR),
        record_video_size={"width": 1280, "height": 720},
    )
    page = context.new_page()
    page.goto(URL, wait_until="networkidle")
    page.add_style_tag(content="""
      html { scroll-behavior: smooth; }
      #demo-caption { position:fixed; z-index:9999; left:260px; right:24px; bottom:18px;
        display:flex; gap:14px; align-items:center; padding:12px 16px; border:1px solid #57dfae;
        border-radius:10px; background:rgba(6,15,28,.94); box-shadow:0 10px 30px rgba(0,0,0,.38);
        transition:opacity .35s ease; pointer-events:none; }
      #demo-caption strong { color:#57dfae; font-size:15px; white-space:nowrap; }
      #demo-caption span { color:#d8e2f2; font-size:13px; }
      #demo-intro { position:fixed; inset:0; z-index:10000; display:flex; flex-direction:column;
        align-items:center; justify-content:center; text-align:center; background:#08101f;
        transition:opacity .7s ease; }
      #demo-intro h1 { margin:0; font-size:48px; color:#f3f6ff; }
      #demo-intro p { margin:12px 0 0; max-width:760px; color:#9eacc3; font-size:20px; }
      #demo-intro b { color:#57dfae; }
    """)
    page.evaluate("""() => {
      const cap=document.createElement('div'); cap.id='demo-caption';
      cap.innerHTML='<strong></strong><span></span>'; document.body.appendChild(cap);
      const intro=document.createElement('div'); intro.id='demo-intro';
      intro.innerHTML='<h1>FPL Intelligence</h1><p>A <b>five-gameweek</b>, scenario-aware decision workspace</p>';
      document.body.appendChild(intro);
    }""")

    pause(page, 4.5)
    page.evaluate("document.getElementById('demo-intro').style.opacity='0'")
    pause(page, 0.8)
    page.evaluate("document.getElementById('demo-intro').remove()")

    caption(page, "ATTENTION FIRST", "Official freshness, material changes, and the next decision in one local workspace.")
    pause(page, 5.5)

    page.locator('[data-view="decisions"]').click()
    pause(page, 1.2)
    caption(page, "THREE RISK PROFILES", "Compare Conservative, Balanced, and Aggressive recommendations without changing the FPL account.")
    pause(page, 6.0)

    smooth_to(page, '#weekly-profile-options', -105)
    caption(page, "ACTUAL FREE TRANSFERS", "This demo starts with 2 available transfers. Five is a cap, never an assumption.")
    pause(page, 6.5)

    smooth_to(page, '#weekly-plan', -90)
    caption(page, "FIVE-GAMEWEEK LOOKAHEAD", "Rolling, transfers, hits, bank, and future flexibility are compared over reachable states.")
    pause(page, 7.5)

    details = page.locator('#weekly-plan details')
    if details.count():
        details.click()
    caption(page, "NEXT ACTION ONLY", "Future moves are provisional conditions, not a rigid transfer script.")
    pause(page, 6.5)

    smooth_to(page, '#weekly-lineup', -80)
    caption(page, "POST-DECISION TEAM", "See the XI, bench, captaincy, formation, and 1 / 3 / 5-gameweek xPts together.")
    pause(page, 7.0)

    page.locator('[data-weekly-profile="aggressive"]').click()
    smooth_to(page, '#weekly-profile-options', -100)
    caption(page, "SCENARIO COMPARISON", "Switch risk profiles instantly and inspect each planner edge against rolling.")
    pause(page, 5.5)

    page.locator('[data-view="fixtures"]').click()
    pause(page, 1.0)
    caption(page, "OFFICIAL FIXTURES", "Gameweek filters, fixture difficulty, blanks, and doubles feed the projection horizon.")
    pause(page, 5.5)

    page.locator('[data-view="model"]').click()
    pause(page, 1.0)
    caption(page, "TRANSPARENT SOURCES", "Model status and source provenance stay inspectable. Refresh runs only on demand.")
    pause(page, 5.5)

    page.evaluate("""() => {
      const outro=document.createElement('div'); outro.id='demo-intro';
      outro.innerHTML='<h1>One compact weekly decision</h1><p>Built for a <b>top-50k objective</b> in under 15 minutes a week</p>';
      document.body.appendChild(outro);
    }""")
    pause(page, 5.0)

    video = page.video
    context.close()
    raw_path = Path(video.path())
    final_raw = ROOT / "fpl-intelligence-demo-raw.webm"
    if final_raw.exists():
        final_raw.unlink()
    raw_path.replace(final_raw)
    browser.close()
    print(final_raw)
