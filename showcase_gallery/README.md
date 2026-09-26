# Showcase gallery (public)

Where your Monday / Wednesday / Friday creations live. It is **separate from the control panel**: this one is public, and the panel is private.

## Deploy once
1. Create a **public** GitHub repo (e.g. `btc-builds`) and put the contents of this folder at its root.
2. Go to [share.streamlit.io](https://share.streamlit.io), click **Create app**, pick the repo, and set the main file to `gallery_app.py`.
3. Your gallery is live at `https://<name>.streamlit.app`.

## Add a build
1. In the panel's Build Lab, open an idea, click **🧩 Build prompt**, and paste it into Claude Code or Codex.
2. The agent writes `builds/<slug>.py`. Run it locally to check it:
   ```
   streamlit run gallery_app.py
   ```
3. Commit and push. The page appears at `https://<name>.streamlit.app/<slug>`.
4. Back in Build Lab, paste that link into **Shipped link** and set the status to **✅ Ready**. The next showcase slot will write the launch post around it.

GIFs and videos: attach the file directly to the X post, and optionally embed it on the build page too.
