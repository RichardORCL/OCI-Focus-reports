@echo off
rem Copy this file to runall.bat (not tracked in git) and adjust if needed.
rem Settings such as report location and upload bucket are in config.json.
echo Running all data...
python getFocusReports.py -update
rem First run: build the consolidated reports now, so the next steps have data to work with.
if not exist FocusReports\ConsolidatedCSV python makeDailyReports.py
python getStorageReport.py
python getResourceNames.py
python getRelationships.py
echo Making all the reports...
python makeDailyReports.py
python makePerUserReport.py
python makeStorageReport.py
python makeHomeReport.py
rem Upload the HTML reports to the bucket in config.json upload.bucket (skipped when empty)
python uploadReports.py
