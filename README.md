# Service Area Checker

Enter an origin (address or ZIP) and a list of ZIP codes. The app shows each ZIP on a map
(green = within the max drive time, red = outside), and clicking a ZIP shows drive time and miles
and lets you remove it.

## Deploy on Streamlit Community Cloud

1. Create a GitHub repo and upload `app.py`, `requirements.txt`, `README.md`.
2. Go to https://share.streamlit.io and sign in with GitHub.
3. Click **Create app** -> pick the repo -> branch `main` -> main file `app.py` -> **Deploy**.

## Run locally

```
pip install -r requirements.txt
streamlit run app.py
```

## Notes

- Drive times use the public OSRM demo server (no live traffic) to the center of each ZIP.
- ZIP boundaries are loaded from a public Texas GeoJSON; ZIPs outside Texas or missing from it
  are drawn as a small circle around the ZIP center.
- No API keys needed.
