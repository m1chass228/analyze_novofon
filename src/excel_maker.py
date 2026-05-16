import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
import json
import os

def create_call_report(gemini_json_str: str, call_id: str, duration_sec: int) -> str:
    """
    Принимает сырой JSON-текст от Gemini, ID звонка и длительность.
    Генерирует красивый Excel-отчет.
    """
    try:
        data = json.loads(gemini_json_str)
    except Exception as e:
        print(f"Ошибка парсинга JSON от Gemini: {e}")
        return ""

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Call {call_id}"
    
    # Включаем сетку
    ws.views.sheetView[0].showGridLines = True

    # Стили
    font_title = Font(name="Calibri", size=16, bold=True, color="1F497D")
    font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_sub_header = Font(name="Calibri", size=11, bold=True, color="1F497D")
    font_bold = Font(name="Calibri", size=11, bold=True)
    font_regular = Font(name="Calibri", size=11)
    font_italic = Font(name="Calibri", size=10, italic=True, color="595959")

    fill_header = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    fill_summary = PatternFill(start_color="DCE6F1", end_color="DCE6F1", fill_type="solid")
    fill_bad_score = PatternFill(start_color="FDE9D9", end_color="FDE9D9", fill_type="solid")

    thin_border_side = Side(border_style="thin", color="D9D9D9")
    thin_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    double_bottom = Border(top=Side(border_style="thin", color="1F497D"), bottom=Side(border_style="double", color="1F497D"))

    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")
    align_wrap_left = Alignment(horizontal="left", vertical="top", wrap_text=True)

    # 1. Шапка
    ws["A1"] = "АУДИТ КОНТРОЛЯ КАЧЕСТВА ЗВОНКОВ"
    ws["A1"].font = font_title
    ws.row_dimensions[1].height = 25

    # 2. Мета-информация
    ws["A3"] = "ID Звонка:"
    ws["B3"] = call_id
    ws["A4"] = "Длительность:"
    ws["B4"] = f"{duration_sec} сек. (~{round(duration_sec/60, 1)} мин)"
    ws["A5"] = "Итоговый балл (из JSON):"
    ws["B5"] = data.get("total_score", 0)
    ws["A6"] = "Категория:"
    ws["B6"] = f"Категория {data.get('category', '')}"

    for r in range(3, 7):
        ws[f"A{r}"].font = font_bold
        ws[f"B{r}"].font = font_regular
    ws["B5"].font = Font(name="Calibri", size=11, bold=True, color="2E743E")

    # 3. Резюме звонка
    ws.merge_cells("A8:D8")
    ws["A8"] = "КРАТКОЕ РЕЗЮМЕ ЗВОНКА (ИТОГ)"
    ws["A8"].font = font_sub_header
    ws["A8"].fill = fill_summary
    ws.row_dimensions[8].height = 20

    ws.merge_cells("A9:D9")
    ws["A9"] = data.get("summary", "Нет описания")
    ws["A9"].font = font_italic
    ws["A9"].alignment = align_wrap_left
    ws.row_dimensions[9].height = 40

    # 4. Пересказ диалога
    ws.merge_cells("A11:D11")
    ws["A11"] = "КРАТКИЙ ПЕРЕСКАЗ ДИАЛОГА"
    ws["A11"].font = font_sub_header
    ws["A11"].fill = fill_summary
    ws.row_dimensions[11].height = 20

    ws.merge_cells("A12:D12")
    ws["A12"] = data.get("dialog_overview", "Нет пересказа")
    ws["A12"].font = font_regular
    ws["A12"].alignment = align_wrap_left
    ws.row_dimensions[12].height = 50

    # 5. Таблица критериев
    headers = ["Критерий оценки", "Набранный балл", "Макс. балл", "Комментарий аудитора"]
    start_row = 15

    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=col_idx, value=header)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center if col_idx in [2, 3] else align_left
    ws.row_dimensions[start_row].height = 25

    # Маппинг ключей из JSON на читаемые названия критериев и их макс. баллы
    criteria_mapping = {
        "contact": ("Установление контакта", 2),
        "name_usage": ("Использование имени", 2),
        "needs_analysis": ("Выяснение потребностей", 9),
        "presentation": ("Презентация услуг", 7),
        "pricing": ("Презентация цен", 3),
        "objections": ("Работа с возражениями", 4),
        "closing_to_appointment": ("Ведение к записи", 8),
        "termination": ("Завершение диалога", 3),
        "individual_approach": ("Индивидуальный подход", 5)
    }

    current_row = start_row + 1
    details = data.get("details", {})

    for key, (label, max_score) in criteria_mapping.items():
        criterion_data = details.get(key, {"score": 0, "comment": ""})
        score = criterion_data.get("score", 0)
        comment = criterion_data.get("comment", "")

        ws.cell(row=current_row, column=1, value=label).font = font_bold
        ws.cell(row=current_row, column=2, value=score).font = font_regular
        ws.cell(row=current_row, column=3, value=max_score).font = font_regular
        ws.cell(row=current_row, column=4, value=comment).font = font_regular

        ws.cell(row=current_row, column=1).alignment = align_left
        ws.cell(row=current_row, column=2).alignment = align_center
        ws.cell(row=current_row, column=3).alignment = align_center
        ws.cell(row=current_row, column=4).alignment = align_wrap_left

        # Подсветка слабых мест (если балл 0)
        for c in range(1, 5):
            cell = ws.cell(row=current_row, column=c)
            cell.border = thin_border
            if score == 0:
                cell.fill = fill_bad_score

        ws.row_dimensions[current_row].height = 22
        current_row += 1

    # 6. Строка ИТОГО с формулами Excel
    ws.cell(row=current_row, column=1, value="ИТОГО (ФОРМУЛА):").font = font_bold
    ws.cell(row=current_row, column=1).alignment = align_right

    ws.cell(row=current_row, column=2, value=f"=SUM(B{start_row+1}:B{current_row-1})").font = font_bold
    ws.cell(row=current_row, column=2).alignment = align_center

    ws.cell(row=current_row, column=3, value=f"=SUM(C{start_row+1}:C{current_row-1})").font = font_bold
    ws.cell(row=current_row, column=3).alignment = align_center

    for c in range(1, 5):
        ws.cell(row=current_row, column=c).border = double_bottom
    ws.row_dimensions[current_row].height = 25

    # Ширина колонок
    column_widths = {"A": 30, "B": 18, "C": 15, "D": 75}
    for col, width in column_widths.items():
        ws.column_dimensions[col].width = width

    os.makedirs("reports", exist_ok=True)
    file_name = f"reports/report_{call_id}.xlsx"
    wb.save(file_name)
    return file_name