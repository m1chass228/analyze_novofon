import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
import json
import os
from datetime import datetime

# Пути к директориям
BASE_REPORTS_DIR = "reports"
INDIVIDUAL_DIR = os.path.join(BASE_REPORTS_DIR, "individual")
MASTER_REPORT_PATH = os.path.join(BASE_REPORTS_DIR, "master_calls_report.xlsx")

def init_report_structure():
    """Создает необходимую структуру папок"""
    os.makedirs(INDIVIDUAL_DIR, exist_ok=True)

def create_call_report(gemini_json_str: str, call_id: str, duration_sec: int, record_url: str) -> str:
    try:
        data = json.loads(gemini_json_str)
    except Exception as e:
        print(f"Ошибка парсинга JSON: {e}")
        return ""

    init_report_structure()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Call {call_id}"
    ws.views.sheetView[0].showGridLines = True

    # Стили шрифтов те же...
    font_title = Font(name="Calibri", size=16, bold=True, color="1F497D")
    font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_sub_header = Font(name="Calibri", size=11, bold=True, color="1F497D")
    font_bold = Font(name="Calibri", size=11, bold=True)
    font_regular = Font(name="Calibri", size=11)
    font_italic = Font(name="Calibri", size=10, italic=True, color="595959")
    font_link = Font(name="Calibri", size=11, color="0563C1", underline="single")

    fill_header = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    fill_summary = PatternFill(start_color="DCE6F1", end_color="DCE6F1", fill_type="solid")
    fill_bad_score = PatternFill(start_color="FDE9D9", end_color="FDE9D9", fill_type="solid")
    thin_border = Border(left=Side(style="thin", color="D9D9D9"), right=Side(style="thin", color="D9D9D9"),
                         top=Side(style="thin", color="D9D9D9"), bottom=Side(style="thin", color="D9D9D9"))
    double_bottom = Border(top=Side(style="thin", color="1F497D"), bottom=Side(style="double", color="1F497D"))
    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    align_wrap_left = Alignment(horizontal="left", vertical="top", wrap_text=True)

    ws["A1"] = "АУДИТ КОНТРОЛЯ КАЧЕСТВА ЗВОНКОВ"
    ws["A1"].font = font_title
    ws.row_dimensions[1].height = 25

    # Заполняем метаданные (добавили Администратора и Филиал)
    metadata = [
        ("ID Звонка:", call_id),
        ("Длительность:", f"{duration_sec} сек. (~{round(duration_sec/60, 1)} мин)"),
        ("Администратор:", data.get("admin_name", "Не определен")),
        ("Филиал клиники:", data.get("clinic_branch", "Не определен")),
        ("Итоговый балл:", data.get("total_score", 0)),
        ("Категория:", f"Категория {data.get('category', '')}"),
    ]

    for i, (label, val) in enumerate(metadata, start=3):
        ws[f"A{i}"] = label
        ws[f"B{i}"] = val
        ws[f"A{i}"].font = font_bold
        ws[f"B{i}"].font = font_regular

    # Подсвечиваем балл
    ws["B7"].font = Font(name="Calibri", size=11, bold=True, color="2E743E") # это будет ячейка Итоговый балл (строка 7)

    ws["A9"] = "Запись разговора:"
    if record_url:
        ws["B9"] = "Слушать запись"; ws["B9"].hyperlink = record_url; ws["B9"].font = font_link
    else:
        ws["B9"] = "Ссылка отсутствует"; ws["B9"].font = font_italic
    ws["A9"].font = font_bold

    # Блок резюме смещаем на строку 11
    ws.merge_cells("A11:D11"); ws["A11"] = "КРАТКОЕ РЕЗЮМЕ ЗВОНКА (ИТОГ)"; ws["A11"].font = font_sub_header; ws["A11"].fill = fill_summary
    ws.merge_cells("A12:D12"); ws["A12"] = data.get("summary", "Нет описания"); ws["A12"].font = font_italic; ws["A12"].alignment = align_wrap_left; ws.row_dimensions[12].height = 40

    # Блок пересказа смещаем на строку 14
    ws.merge_cells("A14:D14"); ws["A14"] = "КРАТКИЙ ПЕРЕСКАЗ ДИАЛОГА"; ws["A14"].font = font_sub_header; ws["A14"].fill = fill_summary
    ws.merge_cells("A15:D15"); ws["A15"] = data.get("dialog_overview", "Нет пересказа"); ws["A15"].font = font_regular; ws["A15"].alignment = align_wrap_left; ws.row_dimensions[15].height = 50

    headers = ["Критерий оценки", "Набранный балл", "Макс. балл", "Комментарий аудитора"]
    start_row = 17 # Таблица критериев теперь стартует с 17 строки
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=col_idx, value=header)
        cell.font = font_header; cell.fill = fill_header; cell.alignment = align_center if col_idx in [2, 3] else align_left
    ws.row_dimensions[start_row].height = 25

    criteria_mapping = {
        "contact": ("Установление контакта", 2), "name_usage": ("Использование имени", 2),
        "needs_analysis": ("Выяснение потребностей", 9), "presentation": ("Презентация услуг", 7),
        "pricing": ("Презентация цен", 3), "objections": ("Работа с возражениями", 4),
        "closing_to_appointment": ("Ведение к записи", 8), "termination": ("Завершение диалога", 3),
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

        for c in range(1, 5):
            cell = ws.cell(row=current_row, column=c); cell.border = thin_border
            if score == 0: cell.fill = fill_bad_score
        ws.row_dimensions[current_row].height = 22
        current_row += 1

    ws.cell(row=current_row, column=1, value="ИТОГО (ФОРМУЛА):").font = font_bold
    ws.cell(row=current_row, column=1).alignment = Alignment(horizontal="right", vertical="center")
    ws.cell(row=current_row, column=2, value=f"=SUM(B{start_row+1}:B{current_row-1})").font = font_bold
    ws.cell(row=current_row, column=2).alignment = align_center
    ws.cell(row=current_row, column=3, value=f"=SUM(C{start_row+1}:C{current_row-1})").font = font_bold
    ws.cell(row=current_row, column=3).alignment = align_center

    for c in range(1, 5): ws.cell(row=current_row, column=c).border = double_bottom
    ws.row_dimensions[current_row].height = 25
    for col, width in {"A": 30, "B": 18, "C": 15, "D": 75}.items(): ws.column_dimensions[col].width = width

    file_name = os.path.join(INDIVIDUAL_DIR, f"report_{call_id}.xlsx")
    wb.save(file_name)
    return file_name

def update_master_report(calls_data: list) -> str:
    """Обновленный мастер-отчет. Добавлена колонка Филиал клиники"""
    init_report_structure()
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сводный журнал"
    ws.views.sheetView[0].showGridLines = True

    font_title = Font(name="Calibri", size=16, bold=True, color="1F497D")
    font_header = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    font_regular = Font(name="Calibri", size=10)
    font_bold = Font(name="Calibri", size=10, bold=True)
    font_link = Font(name="Calibri", size=10, color="0563C1", underline="single")
    
    fill_header = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    thin_border = Border(left=Side(style="thin", color="D9D9D9"), right=Side(style="thin", color="D9D9D9"),
                         top=Side(style="thin", color="D9D9D9"), bottom=Side(style="thin", color="D9D9D9"))
    
    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center")

    ws["A1"] = "СВОДНЫЙ ЖУРНАЛ АУДИТА ЗВОНКОВ (С ДЕТАЛИЗАЦИЕЙ БАЛЛОВ)"
    ws["A1"].font = font_title
    ws.row_dimensions[1].height = 25

    # ДОБАВИЛИ ФИЛИАЛ КЛИНИКИ
    headers = [
        "ID Звонка", "Дата", "Время", "Длительность (сек)", "Итоговый балл", 
        "Администратор", "Филиал клиники", "Контакт (2)", "Имя (2)", "Потребности (9)", 
        "Презентация (7)", "Цены (3)", "Возражения (4)", "Запись (8)", 
        "Финал (3)", "Подход (5)", "Краткое резюме", "Файл отчета", "Запись разговора"
    ]
    
    header_row = 3
    for col_idx, text in enumerate(headers, 1):
        cell = ws.cell(row=header_row, column=col_idx, value=text)
        cell.font = font_header; cell.fill = fill_header; cell.alignment = align_center
    ws.row_dimensions[header_row].height = 28

    current_row = header_row + 1
    criteria_keys = [
        "contact", "name_usage", "needs_analysis", "presentation", 
        "pricing", "objections", "closing_to_appointment", "termination", "individual_approach"
    ]

    # Разбираем кортеж из get_success_calls_for_master()
    for start_time_str, call_id, duration, admin_name, clinic_branch, analysis_text, record_url in calls_data:
        call_date, call_time = "-", "-"
        if start_time_str:
            try:
                dt_obj = datetime.strptime(start_time_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
                call_date = dt_obj.strftime("%d.%m.%Y")
                call_time = dt_obj.strftime("%H:%M:%S")
            except:
                parts = start_time_str.split(" ")
                if len(parts) == 2: call_date, call_time = parts[0], parts[1]

        total_score = 0
        summary_text = "Нет описания"
        scores_dict = {k: 0 for k in criteria_keys}

        if analysis_text:
            try:
                parsed = json.loads(analysis_text)
                total_score = parsed.get("total_score", 0)
                summary_text = parsed.get("summary", "")
                details = parsed.get("details", {})
                for k in criteria_keys:
                    scores_dict[k] = details.get(k, {}).get("score", 0)
            except:
                pass

        # Заполнение
        ws.cell(row=current_row, column=1, value=str(call_id)).alignment = align_center
        ws.cell(row=current_row, column=2, value=call_date).alignment = align_center
        ws.cell(row=current_row, column=3, value=call_time).alignment = align_center
        ws.cell(row=current_row, column=4, value=int(duration or 0)).alignment = align_center
        ws.cell(row=current_row, column=5, value=total_score).alignment = align_center
        ws.cell(row=current_row, column=6, value=str(admin_name)).alignment = align_left
        ws.cell(row=current_row, column=7, value=str(clinic_branch)).alignment = align_left # НАШ ФИЛИАЛ
        
        # Критерии уехали на шаг вперед (колонки 8-16)
        for idx, key in enumerate(criteria_keys, start=8):
            ws.cell(row=current_row, column=idx, value=scores_dict[key]).alignment = align_center

        # Резюме (колонка 17)
        cell_summary = ws.cell(row=current_row, column=17, value=summary_text)
        cell_summary.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

        # Ссылка на индивидуальный эксель (колонка 18)
        local_report_rel_path = f"individual/report_{call_id}.xlsx"
        cell_file = ws.cell(row=current_row, column=18, value="Открыть Excel")
        cell_file.hyperlink = local_report_rel_path; cell_file.font = font_link; cell_file.alignment = align_center

        # Ссылка на аудио (колонка 19)
        cell_link = ws.cell(row=current_row, column=19, value="Слушать запись")
        if record_url:
            cell_link.hyperlink = record_url; cell_link.font = font_link
        else:
            cell_link.value = "Отсутствует"; cell_link.font = Font(name="Calibri", size=10, italic=True, color="A6A6A6")
        cell_link.alignment = align_center

        # Бордеры
        for col in range(1, 20):
            c = ws.cell(row=current_row, column=col); c.border = thin_border
            if col in [5, 1]: c.font = font_bold
            elif col not in [18, 19]: c.font = font_regular

        ws.row_dimensions[current_row].height = 24
        current_row += 1

    # Ширина колонок (всего 19)
    widths = {
        "A": 14, "B": 12, "C": 12, "D": 15, "E": 14, "F": 18, "G": 22, # G стал шире под адрес/филиал
        "H": 11, "I": 11, "J": 13, "K": 13, "L": 11, "M": 12, "N": 12, "O": 11, "P": 11,
        "Q": 55, "R": 16, "S": 18
    }
    for col_letter, w in widths.items(): ws.column_dimensions[col_letter].width = w

    wb.save(MASTER_REPORT_PATH)
    return MASTER_REPORT_PATH