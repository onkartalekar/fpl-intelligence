from pathlib import Path
import shutil
import subprocess
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8878/dashboard-demo.html"
VIDEO_DIR = ROOT / "raw-video"
VIDEO_DIR.mkdir(exist_ok=True)
OUTPUT = ROOT / "fpl-intelligence-demo.mp4"
NARRATION = ROOT / "narration.mp3"


def pause(page, seconds):
    page.wait_for_timeout(max(0, int(seconds * 1000)))


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


def show_view(page, name):
    page.evaluate("name => showView(name)", name)
    page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
    pause(page, 0.8)


def smooth_to(page, selector, offset=-85):
    page.evaluate(
        """([selector, offset]) => {
          const el = document.querySelector(selector);
          if (!el) throw new Error(`Demo target not found: ${selector}`);
          const top = el.getBoundingClientRect().top + window.scrollY + offset;
          window.scrollTo({top, behavior: 'smooth'});
        }""",
        [selector, offset],
    )
    pause(page, 1.0)


def scene(page, title, body, seconds, action=None):
    started = time.monotonic()
    if action:
        action()
    caption(page, title, body)
    pause(page, seconds - (time.monotonic() - started))


def intro(page):
    page.evaluate(
        """() => {
          const intro=document.createElement('div'); intro.id='demo-intro';
          intro.innerHTML='<h1>FPL Intelligence</h1><p>Every feature in one <b>source-backed</b> weekly decision workspace</p>';
          document.body.appendChild(intro);
        }"""
    )
    pause(page, 5.2)
    page.evaluate("document.getElementById('demo-intro').style.opacity='0'")
    pause(page, 0.8)
    page.evaluate("document.getElementById('demo-intro').remove()")


def outro(page):
    page.evaluate(
        """() => {
          const outro=document.createElement('div'); outro.id='demo-intro';
          outro.innerHTML='<h1>One compact weekly decision</h1><p>Evidence, uncertainty, and limitations stay <b>visible</b></p>';
          document.body.appendChild(outro);
        }"""
    )
    pause(page, 7.0)


def show_reminder_preview(page):
    page.evaluate(
        """() => {
          const card=document.createElement('section'); card.id='demo-feature-card';
          card.innerHTML=`
            <div class="eyebrow">Opt-in automation · GitHub Actions</div>
            <h2>Deadline reminder email</h2>
            <div class="demo-feature-pills"><span>Hourly check</span><span>Per-team lead time</span><span>Dry-run safe</span></div>
            <div class="demo-email">
              <strong>FPL reminder: GW2 deadline in ~3h</strong>
              <span>Recommended action: Make two transfers</span>
              <span>Captain: B. Fernandes · Vice-captain: Semenyo</span>
              <span>Projected points, bank, and free transfers included</span>
            </div>
            <p>Recipient and SMTP settings stay in repository secrets. Public workflow logs never print addresses, credentials, or dry-run email bodies.</p>`;
          document.body.appendChild(card);
        }"""
    )


def hide_reminder_preview(page):
    page.evaluate("document.getElementById('demo-feature-card')?.remove()")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1280, "height": 720},
        device_scale_factor=1,
        record_video_dir=str(VIDEO_DIR),
        record_video_size={"width": 1280, "height": 720},
    )
    page = context.new_page()
    page.add_init_script("localStorage.setItem('fpl-theme', 'dark')")
    page.goto(URL, wait_until="networkidle")
    page.evaluate("showView('overview')")
    page.add_style_tag(
        content="""
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
      #demo-feature-card { position:fixed; z-index:9998; left:300px; right:66px; top:86px;
        padding:28px; border:1px solid #57dfae; border-radius:14px; background:#101b2e;
        color:#f3f6ff; box-shadow:0 18px 50px rgba(0,0,0,.5); }
      #demo-feature-card h2 { margin:4px 0 14px; font-size:28px; }
      #demo-feature-card p { color:#9eacc3; margin:16px 0 0; }
      .demo-feature-pills { display:flex; gap:8px; margin-bottom:16px; }
      .demo-feature-pills span { border:1px solid #293a58; border-radius:999px; padding:6px 10px; color:#57dfae; }
      .demo-email { display:grid; gap:8px; padding:18px; border-radius:10px; background:#08101f; border:1px solid #293a58; }
      .demo-email strong { color:#57dfae; font-size:17px; }
      .demo-email span { color:#d8e2f2; }
    """
    )
    page.evaluate(
        """() => {
          const cap=document.createElement('div'); cap.id='demo-caption';
          cap.innerHTML='<strong></strong><span></span>'; document.body.appendChild(cap);
        }"""
    )

    intro(page)
    scene(
        page,
        "ATTENTION FIRST",
        "Season readiness, material changes, source freshness, and manual refresh status stay together.",
        9,
        lambda: show_view(page, "overview"),
    )
    scene(
        page,
        "MY TEAM",
        "Look up a public team ID and inspect the published squad, captaincy, value, bank, and connection health.",
        10,
        lambda: show_view(page, "squad"),
    )
    scene(
        page,
        "LEGAL DRAFT BUILDER",
        "Declare a preseason squad with live budget, position, squad-size, and three-per-club validation.",
        9,
        lambda: smooth_to(page, "#draft-squad-panel", -70),
    )
    scene(
        page,
        "LOCAL PROFILE",
        "Team ID, timezone, risk preference, and confirmed free transfers personalize recommendations without a password.",
        8,
        lambda: show_view(page, "profile"),
    )
    scene(
        page,
        "THREE STRATEGY PROFILES",
        "Compare Conservative, Balanced, and Aggressive squads using one, three, and five-gameweek expected points.",
        10,
        lambda: show_view(page, "decisions"),
    )
    scene(
        page,
        "NEXT ACTION ONLY",
        "The weekly decision uses actual free transfers, point costs, bank, captaincy, and future flexibility.",
        11,
        lambda: smooth_to(page, "#weekly-profile-options", -100),
    )
    scene(
        page,
        "FIVE-GAMEWEEK PLANNER",
        "Rolling has explicit option value, while future moves remain conditional branches rather than a rigid script.",
        10,
        lambda: smooth_to(page, "#weekly-plan", -85),
    )
    scene(
        page,
        "POST-DECISION XI",
        "Formation, starting eleven, bench order, captaincy, uncertainty, rotation risk, and player detail stay connected.",
        10,
        lambda: smooth_to(page, "#weekly-lineup", -75),
    )

    def filter_players():
        show_view(page, "players")
        page.locator("#player-search").fill("Raya")
        pause(page, 0.5)

    scene(
        page,
        "PLAYER EXPLORER",
        "Search and filter official prices, ownership, availability, positions, and clubs.",
        8,
        filter_players,
    )

    def advance_fixture():
        show_view(page, "fixtures")
        button = page.locator("#fixture-gameweek-next")
        if button.count() and button.is_enabled():
            button.click()
            pause(page, 0.5)

    scene(
        page,
        "OFFICIAL FIXTURES",
        "Navigate gameweeks and clubs while tracking kickoff times, blanks, doubles, and official difficulty.",
        8,
        advance_fixture,
    )

    def inspect_transfer():
        show_view(page, "transfers")
        page.locator("#relevance-filter").select_option("all")
        page.locator("#freshness-filter").evaluate(
            "el => { el.value = 'all'; el.dispatchEvent(new Event('change', {bubbles: true})); }"
        )
        first = page.locator("#feed .transfer").first
        if first.count():
            first.click()
            pause(page, 0.5)

    scene(
        page,
        "FIRST-PARTY TRANSFER EVIDENCE",
        "Filters, relevance, FPL reconciliation, and the evidence inspector keep every source auditable.",
        10,
        inspect_transfer,
    )
    scene(
        page,
        "MODEL PERFORMANCE",
        "Frozen pre-deadline forecasts are compared with official results across horizons, teams, and players.",
        10,
        lambda: show_view(page, "performance"),
    )
    scene(
        page,
        "SHADOW MODEL",
        "The ML minutes challenger is scored separately against the champion and never changes live recommendations.",
        7,
        lambda: smooth_to(page, "#shadow-models-list", -180),
    )
    scene(
        page,
        "DEADLINE REMINDER EMAILS",
        "An hourly, opt-in GitHub Action sends current advice inside each team's configured lead-time window.",
        7,
        lambda: show_reminder_preview(page),
    )
    hide_reminder_preview(page)
    scene(
        page,
        "MODEL STATUS",
        "Feed readiness, projection version, configured sources, modeling policy, and account boundaries remain inspectable.",
        9,
        lambda: show_view(page, "model"),
    )

    def toggle_theme():
        page.locator("#theme-toggle").click()
        pause(page, 2.5)
        page.locator("#theme-toggle").click()
        pause(page, 0.5)

    scene(
        page,
        "ACCESSIBLE BY DEFAULT",
        "Light and dark themes, responsive controls, keyboard navigation, and manual-only refresh complete the workspace.",
        7,
        toggle_theme,
    )
    outro(page)

    video = page.video
    context.close()
    raw_path = Path(video.path())
    final_raw = ROOT / "fpl-intelligence-demo-raw.webm"
    if final_raw.exists():
        final_raw.unlink()
    raw_path.replace(final_raw)
    browser.close()

ffmpeg = shutil.which("ffmpeg")
if not ffmpeg:
    raise RuntimeError("ffmpeg is required to produce the narrated MP4")
if not NARRATION.exists():
    raise RuntimeError(f"Narration track not found: {NARRATION}")
subprocess.run(
    [
        ffmpeg,
        "-y",
        "-i",
        str(final_raw),
        "-i",
        str(NARRATION),
        "-filter_complex",
        "[1:a]apad[a]",
        "-map",
        "0:v:0",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-shortest",
        str(OUTPUT),
    ],
    check=True,
)
final_raw.unlink()
if VIDEO_DIR.exists() and not any(VIDEO_DIR.iterdir()):
    VIDEO_DIR.rmdir()
print(OUTPUT)
