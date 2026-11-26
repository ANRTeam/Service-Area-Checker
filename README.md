# Service Area Checker


https://service-area-checker-ixzb8it93nu9xnaxwnzspw.streamlit.app/


Enter an origin (address or ZIP) and a list of ZIP codes. The app shows each ZIP on a map
(green = within the max drive time, red = outside), and clicking a ZIP shows drive time and miles
and lets you remove it.




## Notes

- Drive times use the public OSRM demo server (no live traffic) to the center of each ZIP.
- ZIP boundaries are loaded from a public Texas GeoJSON; ZIPs outside Texas or missing from it
  are drawn as a small circle around the ZIP center.
- No API keys needed.
