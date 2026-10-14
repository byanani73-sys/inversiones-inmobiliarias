import gspread
from google.oauth2.service_account import Credentials

scopes = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
ws = gspread.authorize(creds).open_by_key("13fWYaAwwe9qyVqfb08Qhu_zusak_uVvTQv3-1rhfwDA").worksheet("Hoja 1")

headers = ws.row_values(1)
if "activa" not in headers:
    next_col = len(headers) + 1
    ws.update_cell(1, next_col, "activa")
    total_rows = len(ws.col_values(1))
    ws.update(f"{chr(64+next_col)}2:{chr(64+next_col)}{total_rows}", [[1]] * (total_rows - 1))
    print(f"Columna 'activa' agregada en columna {next_col}")
else:
    print("Columna 'activa' ya existe")
