"""HTML rendering for the admin dashboard.

Pure f-string templates — no template engine needed for an internal tool.
Uses the same indigo accent (#6366f1) and system font stack as static/index.html.
"""

_STYLE = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a; color: #e2e8f0; min-height: 100vh;
    display: flex; flex-direction: column; align-items: center;
    padding: 2rem 1rem;
  }
  .container { max-width: 960px; width: 100%; }
  h1 { color: #6366f1; margin-bottom: 0.5rem; }
  .subtitle { color: #94a3b8; margin-bottom: 2rem; font-size: 0.9rem; }
  .cards {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1rem; margin-bottom: 2rem;
  }
  .card {
    background: #1e293b; border-radius: 12px; padding: 1.25rem;
    border: 1px solid #334155;
  }
  .card .label { color: #94a3b8; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }
  .card .value { color: #f1f5f9; font-size: 1.75rem; font-weight: 700; margin-top: 0.25rem; }
  table { width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 12px; overflow: hidden; }
  th { background: #334155; color: #94a3b8; font-size: 0.75rem; text-transform: uppercase;
       letter-spacing: 0.05em; padding: 0.75rem 1rem; text-align: left; }
  td { padding: 0.75rem 1rem; border-top: 1px solid #334155; font-size: 0.9rem; }
  tr:hover td { background: #253349; }
  .status { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }
  .status-ready { background: #064e3b; color: #34d399; }
  .status-registered { background: #1e3a5f; color: #60a5fa; }
  .status-other { background: #3b3523; color: #fbbf24; }
  .header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
  .logout-btn {
    background: #334155; color: #94a3b8; border: none; padding: 0.4rem 1rem;
    border-radius: 8px; cursor: pointer; font-size: 0.85rem;
  }
  .logout-btn:hover { background: #475569; color: #e2e8f0; }
  .login-box {
    background: #1e293b; border-radius: 16px; padding: 3rem;
    border: 1px solid #334155; text-align: center; margin-top: 4rem;
  }
  .login-box h1 { margin-bottom: 1rem; }
  .login-box p { color: #94a3b8; margin-bottom: 2rem; }
  .google-btn {
    display: inline-block; background: #6366f1; color: white; padding: 0.75rem 2rem;
    border-radius: 10px; text-decoration: none; font-weight: 600; font-size: 1rem;
  }
  .google-btn:hover { background: #4f46e5; }
  .error-box { background: #1e293b; border: 1px solid #7f1d1d; border-radius: 16px;
    padding: 3rem; text-align: center; margin-top: 4rem; }
  .error-box h1 { color: #ef4444; }
  .error-box p { color: #94a3b8; margin-top: 1rem; }
  .error-box a { color: #6366f1; }
  .refresh-bar-track {
    position: fixed; top: 0; left: 0; width: 100%; height: 3px;
    background: #1e293b; z-index: 100;
  }
  .refresh-bar {
    height: 100%; width: 100%; background: #6366f1;
    animation: shrink 30s linear forwards;
  }
  @keyframes shrink { from { width: 100%; } to { width: 0%; } }
</style>
"""


def render_login_page() -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Login — RTT Reader</title>{_STYLE}</head>
<body>
<div class="login-box">
  <h1>RTT Reader Admin</h1>
  <p>Sign in to view the dashboard.</p>
  <a href="/admin/login/start" class="google-btn">Sign in with Google</a>
</div>
</body></html>"""


def render_dashboard(stats: list[dict], admin_email: str) -> str:
    total_users = len(stats)
    total_events = sum(s.get("event_count", 0) for s in stats)
    total_messages = sum(s.get("message_count", 0) for s in stats)

    rows = ""
    for s in stats:
        st = s["status"]
        if st == "ready":
            badge = '<span class="status status-ready">ready</span>'
        elif st == "registered":
            badge = '<span class="status status-registered">registered</span>'
        else:
            badge = f'<span class="status status-other">{st}</span>'

        date_range = f'{s["date_min"]} — {s["date_max"]}' if s["date_min"] != "-" else "-"

        rows += f"""<tr>
  <td>{s["name"]}</td>
  <td>{badge}</td>
  <td>{s["message_count"]:,}</td>
  <td>{s["event_count"]:,}</td>
  <td>{date_range}</td>
</tr>
"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Dashboard — RTT Reader</title>
<meta http-equiv="refresh" content="30">
{_STYLE}</head>
<body>
<div class="refresh-bar-track"><div class="refresh-bar"></div></div>
<div class="container">
  <div class="header-row">
    <div>
      <h1>Dashboard</h1>
      <p class="subtitle">Signed in as {admin_email}</p>
    </div>
    <form method="POST" action="/admin/logout">
      <button type="submit" class="logout-btn">Sign out</button>
    </form>
  </div>

  <div class="cards">
    <div class="card"><div class="label">Users</div><div class="value">{total_users}</div></div>
    <div class="card"><div class="label">Total Events</div><div class="value">{total_events:,}</div></div>
    <div class="card"><div class="label">Total Messages</div><div class="value">{total_messages:,}</div></div>
  </div>

  <table>
    <thead><tr>
      <th>Name</th><th>Status</th><th>Messages</th><th>Events</th><th>Date Range</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
</body></html>"""


def render_error_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Error — RTT Reader Admin</title>{_STYLE}</head>
<body>
<div class="error-box">
  <h1>Access Denied</h1>
  <p>{message}</p>
  <p style="margin-top:1.5rem"><a href="/admin/login">Back to login</a></p>
</div>
</body></html>"""
