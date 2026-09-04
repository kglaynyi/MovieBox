"""Compatibility entry point. Deploy a reviewed commit to update MovieBox.

Runtime auto-update is intentionally disabled: it previously erased Git state,
replaced deployed files, and performed network calls before the web port opened.
"""

if __name__ == "__main__":
    print("Runtime auto-update is disabled. Redeploy to update MovieBox.")
