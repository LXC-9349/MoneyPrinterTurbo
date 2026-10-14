# Library Browser UI Fix

## Problem
Users reported that the library database data (1173 person profiles, 4942 articles, 1256 books) was completely invisible in the frontend interface and could not be selected or used.

## Root Cause
The `_render_library_browser()` function was wrapped in a collapsed expander (`expanded=False`), making it invisible to users unless they manually expanded it. Additionally, this function was only called when `video_source == "historical_archive"`, meaning:

1. Users had to first select "历史档案素材（人物传记）" as the video source
2. Then manually click to expand the "Browse Local Library" section
3. Only then would they see the library browser with all the person profiles

## Solution
Changed the expander from `expanded=False` to `expanded=True` on line 1655 of `webui/Main.py`.

### Code Change
```python
# Before:
with st.expander(tr("Browse Local Library"), expanded=False):

# After:
with st.expander(tr("Browse Local Library"), expanded=True):
```

## Result
Now when users:
1. Select "历史档案素材（人物传记）" as the video source
2. The library browser will automatically expand
3. They can immediately see and browse all 1173 person profiles
4. Filter by dynasty or search by keyword
5. View person details including key facts, related articles, and reference books
6. Click "Use This Person" to select them for biography video generation

## Files Modified
- `E:\pj-vd\MoneyPrinterTurbo\webui\Main.py` (line 1655)

## Testing
The application is running at http://127.0.0.1:8502 and Streamlit's hot reload has automatically applied the changes.
