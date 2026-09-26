import streamlit as st

st.set_page_config(page_title="X Control Panel", page_icon="🛰️", layout="wide")

from panel.common import boot, market_strip, password_gate  # noqa: E402

boot()
if not password_gate():
    st.stop()

pages = [
    st.Page("views/feed.py", title="Feed", icon="📰", default=True),
    st.Page("views/build_lab.py", title="Build Lab", icon="🛠"),
    st.Page("views/radar.py", title="Radar", icon="📡"),
    st.Page("views/scoreboard.py", title="Scoreboard", icon="📊"),
    st.Page("views/control_room.py", title="Control Room", icon="⚙️"),
]
nav = st.navigation(pages, position="top")
market_strip()
nav.run()
