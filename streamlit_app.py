import streamlit as st

st.set_page_config(page_title="X Control Panel", page_icon="panel/icons/app.png", layout="wide")

from panel.common import boot, load_secrets, market_strip, owner_bar, switched_off  # noqa: E402
from xcp import config  # noqa: E402

load_secrets()
if not config.live():  # README → Switched off
    switched_off()
    st.stop()
boot()

pages = [
    st.Page("views/monitor.py", title="Monitor", icon="🏮", default=True),
    st.Page("views/feed.py", title="Feed", icon="📜"),
    st.Page("views/build_lab.py", title="Build Lab", icon="⚒️"),
    st.Page("views/scoreboard.py", title="Scoreboard", icon="🧮"),
    st.Page("views/control_room.py", title="Control Room", icon="🏯"),
]
nav = st.navigation(pages, position="top")
owner_bar()
market_strip()
nav.run()
