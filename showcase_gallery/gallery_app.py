"""Public gallery for Build Lab creations. Every file in builds/ becomes a page with its own URL:
https://<your-app>.streamlit.app/<file_name_without_.py>  (the link you put in the showcase post)."""
import ast
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Builds", page_icon="🟧", layout="wide")
BUILDS = Path(__file__).parent / "builds"


def build_meta(path: Path) -> tuple[str, str]:
    """Title and blurb come from the build file's docstring: 'Title\\n\\nBlurb...'."""
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or path.stem.replace("_", " ").title()
    title, _, blurb = doc.partition("\n")
    return title.strip(), blurb.strip()


build_files = sorted(p for p in BUILDS.glob("*.py") if not p.name.startswith("_"))
pages = {p.stem: st.Page(str(p), title=build_meta(p)[0], url_path=p.stem) for p in build_files}


def home():
    st.title("🟧 Builds")
    st.caption("Simulations, trackers and toys about Bitcoin, digital credit and frontier AI.")
    cols = st.columns(2)
    for i, p in enumerate(build_files):
        title, blurb = build_meta(p)
        with cols[i % 2].container(border=True):
            st.page_link(pages[p.stem], label=title, icon="▶️")
            st.caption(blurb[:220])


nav = st.navigation([st.Page(home, title="All builds", icon="🏠", default=True), *pages.values()], position="top")
nav.run()
