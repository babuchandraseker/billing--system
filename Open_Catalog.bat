@echo off
:: ═══════════════════════════════════════════════════════
:: DEPRECATED — use Open_Catalog_PDF.bat instead
:: This file now redirects to the PDF/print catalog.
:: ═══════════════════════════════════════════════════════

echo [NOTE] Open_Catalog.bat is deprecated.
echo        Launching Open_Catalog_PDF.bat instead...
echo.

start "" "http://localhost:5000/catalog/print"
timeout /t 1 /nobreak >nul
