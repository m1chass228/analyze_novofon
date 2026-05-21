import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import json
import os
from datetime import datetime

# Базовые пути для хранения отчетов
BASE_REPORTS_DIR = "reports"
INDIVIDUAL_DIR = os.path.join(BASE_REPORTS_DIR, "individual")
MASTER_REPORT_PATH = os.path.join(BASE_REPORTS_DIR, "master_calls_report.xlsx")


def init_report_structure():
    """Создает структуру папок для отчетов"""
    os.makedirs(INDIVIDUAL_DIR, exist_ok=True)


def create_call_report(gemini_json_str: str, call_id: str, duration_sec: int, record_url: str, customer_phone: str = "Не определен") -> str:
    """Создает премиальный индивидуальный отчет по одному звонку"""
    try:
        data = json.loads(gemini_json_str)
    except Exception as e:
        print(f"Ошибка парсинга JSON: {e}")
        return ""

    init_report_structure()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Аудит Звонка {call_id}"
    
    # Принудительно показываем сетку таблицы (Excel отключает её при заливках ячеек)
    ws.views.sheetView[0].showGridLines = True

    # ==================== ДИЗАЙНЕРСКАЯ ПАЛИТРА СТИЛЕЙ ====================
    font_main_title = Font(name="Segoe UI", size=15, bold=True, color="FFFFFF")
    font_section = Font(name="Segoe UI", size=11, bold=True, color="1A365D")
    font_header = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
    font_bold = Font(name="Segoe UI", size=10, bold=True, color="2C3E50")
    font_regular = Font(name="Segoe UI", size=10, color="2C3E50")
    font_link = Font(name="Segoe UI", size=10, color="1A5276", underline="single", bold=True)

    # Заливки (Пастельные, не режущие глаза тона)
    fill_main_banner = PatternFill(start_color="2C3E50", end_color="2C3E50", fill_type="solid") # Темный графит
    fill_section_bar = PatternFill(start_color="EBF5FB", end_color="EBF5FB", fill_type="solid") # Светло-голубой
    fill_label_side = PatternFill(start_color="F8F9FA", end_color="F8F9FA", fill_type="solid")  # Уютный серый подписи
    
    # Цвета категорий качества
    fill_cat_perfect = PatternFill(start_color="D4EFDF", end_color="D4EFDF", fill_type="solid") # Мягкий зеленый
    fill_cat_good = PatternFill(start_color="E8F8F5", end_color="E8F8F5", fill_type="solid")    # Мягкий мятный
    fill_cat_normal = PatternFill(start_color="FCF3CF", end_color="FCF3CF", fill_type="solid")  # Мягкий желтый
    fill_cat_bad = PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid")     # Мягкий розовый

    # Границы
    border_light = Border(left=Side(style="thin", color="D5D8DC"), right=Side(style="thin", color="D5D8DC"),
                          top=Side(style="thin", color="D5D8DC"), bottom=Side(style="thin", color="D5D8DC"))
    border_total = Border(top=Side(style="thin", color="2C3E50"), bottom=Side(style="double", color="2C3E50"))

    # Выравнивания
    align_center = Alignment(horizontal="center", vertical="center")
    align_left_wrap = Alignment(horizontal="left", vertical="top", wrap_text=True)
    align_label = Alignment(horizontal="left", vertical="center")

    # ==================== ЗАГОЛОВОК ДОКУМЕНТА ====================
    ws.merge_cells("A2:D2")
    title_cell = ws["A2"]
    title_cell.value = f"ОТЧЁТ ПО АУДИТУ КАЧЕСТВА РАЗГОВОРА № {call_id}"
    title_cell.font = font_main_title
    title_cell.fill = fill_main_banner
    title_cell.alignment = align_center
    ws.row_dimensions[2].height = 38

    # Получаем базовые метрики
    score = int(data.get("total_score", 0))
    category = int(data.get("category", 4))

    # Определяем нужный цвет по результатам
    if score >= 37: fill_result = fill_cat_perfect
    elif score >= 30: fill_result = fill_cat_good
    elif score >= 23: fill_result = fill_cat_normal
    else: fill_result = fill_cat_bad

    # ==================== БЛОК МЕТАДАННЫХ (КАРТОЧКА) ====================
    metadata = [
        ("ID звонка", call_id),
        ("Телефон клиента", str(customer_phone)),
        ("Дата анализа", datetime.now().strftime("%d.%m.%Y %H:%M")),
        ("Длительность звонка", f"{duration_sec} сек (~{round(duration_sec/60, 1)} мин)"),
        ("Администратор", data.get("admin_name", "Не определен")),
        ("Филиал клиники", data.get("clinic_branch", "Не определен")),
        ("Итоговый балл", f"{score} / 43"),
        ("Категория качества", f"{category} категория")
    ]

    start_meta_row = 4
    for idx, (label, val) in enumerate(metadata):
        r = start_meta_row + idx
        ws.row_dimensions[r].height = 20
        
        # Левая ячейка (Лейбл)
        c_label = ws.cell(row=r, column=1, value=label)
        c_label.font = font_bold
        c_label.fill = fill_label_side
        c_label.border = border_light
        c_label.alignment = align_label
        
        # Правая ячейка (Значение)
        c_val = ws.cell(row=r, column=2, value=val)
        c_val.font = font_regular
        c_val.border = border_light
        c_val.alignment = align_label
        
        # Подсвечиваем результаты балла и категории в карточке
        if label in ["Итоговый балл", "Категория качества"]:
            c_val.fill = fill_result
            c_val.font = font_bold

    # Ссылка на прослушивание внутри карточки
    r_link = start_meta_row + len(metadata)
    ws.row_dimensions[r_link].height = 22
    cl = ws.cell(row=r_link, column=1, value="Запись разговора")
    cl.font = font_bold
    cl.fill = fill_label_side
    cl.border = border_light
    
    cv = ws.cell(row=r_link, column=2)
    if record_url:
        cv.value = "▶ Слушать аудиозапись"
        cv.hyperlink = record_url
        cv.font = font_link
    else:
        cv.value = "Ссылка отсутствует"
        cv.font = font_regular
    cv.border = border_light

    # ==================== БЛОК: КРАТКОЕ РЕЗЮМЕ ====================
    res_header_row = r_link + 2
    ws.merge_cells(f"A{res_header_row}:D{res_header_row}")
    res_header = ws[f"A{res_header_row}"]
    res_header.value = "КРАТКОЕ РЕЗЮМЕ АУДИТОРА И ИТОГИ ДИАЛОГА"
    res_header.font = font_section
    res_header.fill = fill_section_bar
    res_header.alignment = align_center
    ws.row_dimensions[res_header_row].height = 24

    res_text_row = res_header_row + 1
    ws.merge_cells(f"A{res_text_row}:D{res_text_row+2}") # Объединяем 3 строки для текста
    res_text = ws[f"A{res_text_row}"]
    res_text.value = data.get("summary", "Нет описания резюме.")
    res_text.alignment = align_left_wrap
    res_text.font = font_regular
    
    # Применяем рамку вокруг объединенного блока резюме
    for row in range(res_text_row, res_text_row + 3):
        ws.row_dimensions[row].height = 20
        for col in range(1, 5):
            ws.cell(row=row, column=col).border = border_light

    # ==================== ТАБЛИЦА КРИТЕРИЕВ ОЦЕНКИ ====================
    table_start_row = res_text_row + 4
    headers = ["Критерий чек-листа", "Балл", "Макс.", "Развернутый комментарий аналитика"]
    
    for col_idx, text in enumerate(headers, 1):
        cell = ws.cell(row=table_start_row, column=col_idx, value=text)
        cell.font = font_header
        cell.fill = fill_main_banner
        cell.alignment = align_center
    ws.row_dimensions[table_start_row].height = 26

    # Критерии с жестко вшитым лимитом набранных баллов (до Х б.)
    criteria_map = {
        "contact": ("1. Установление контакта (до 2 б.)", 2),
        "name_usage": ("2. Использование имени (до 2. б.)", 2),
        "needs_analysis": ("3. Выяснение потребностей (до 9 б.)", 9),
        "presentation": ("4. Презентация услуг (до 7 б.)", 7),
        "pricing": ("5. Презентация цен (до 3 б.)", 3),
        "objections": ("6. Работа с возражениями (до 4 б.)", 4),
        "closing_to_appointment": ("7. Ведение к записи (до 8 б.)", 8),
        "termination": ("8. Завершение диалога (до 3 б.)", 3),
        "individual_approach": ("9. Индивидуальный подход (до 5 б.)", 5)
    }

    current_row = table_start_row + 1
    details = data.get("details", {})

    for key, (display_name, max_val) in criteria_map.items():
        ws.row_dimensions[current_row].height = 24
        item = details.get(key, {"score": 0, "comment": ""})
        score_val = item.get("score", 0)
        comment = item.get("comment", "-")

        c1 = ws.cell(row=current_row, column=1, value=display_name)
        c2 = ws.cell(row=current_row, column=2, value=score_val)
        c3 = ws.cell(row=current_row, column=3, value=max_val)
        c4 = ws.cell(row=current_row, column=4, value=comment)

        # Шрифты и выравнивания
        c1.font = font_bold
        c2.font = font_bold
        c2.alignment = align_center
        c3.font = font_regular
        c3.alignment = align_center
        c4.font = font_regular
        c4.alignment = align_left_wrap

        # Светофорная подсветка для критических зон
        if score_val == 0:
            c2.fill = fill_cat_bad  # Жесткий ноль подсвечиваем красным
        elif score_val == max_val:
            c2.fill = fill_cat_perfect # Идеальное выполнение — зеленым

        for col in range(1, 5):
            ws.cell(row=current_row, column=col).border = border_light

        current_row += 1

    # Строка ИТОГО
    ws.row_dimensions[current_row].height = 26
    t1 = ws.cell(row=current_row, column=1, value="ИТОГО РАЗИНТЕГРИРОВАНО:")
    t2 = ws.cell(row=current_row, column=2, value=f"=SUM(B{table_start_row+1}:B{current_row-1})")
    t3 = ws.cell(row=current_row, column=3, value=f"=SUM(C{table_start_row+1}:C{current_row-1})")
    ws.cell(row=current_row, column=4, value="")

    t1.font = font_bold
    t2.font = font_main_title
    t2.fill = fill_main_banner
    t2.alignment = align_center
    t3.font = font_bold
    t3.alignment = align_center

    for col in range(1, 5):
        ws.cell(row=current_row, column=col).border = border_total

    # Фиксируем оптимальную ширину столбцов для идеальной верстки
    ws.column_dimensions['A'].width = 38
    ws.column_dimensions['B'].width = 14
    ws.column_dimensions['C'].width = 10
    ws.column_dimensions['D'].width = 75

    file_name = os.path.join(INDIVIDUAL_DIR, f"report_{call_id}.xlsx")
    wb.save(file_name)
    return file_name


def update_master_report(calls_data: list, public_urls_map: dict = None) -> str:
    """Обновляет Сводный журнал с умной разметкой, фризом шапки и фиксацией квот баллов"""
    if public_urls_map is None:
        public_urls_map = {}

    init_report_structure()
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сводный журнал"
    
    ws.views.sheetView[0].showGridLines = True
    ws.freeze_panes = "A4"

    # ==================== ТЕМАТИЧЕСКИЕ СТИЛИ ====================
    font_title = Font(name="Segoe UI", size=13, bold=True, color="FFFFFF")
    font_header = Font(name="Segoe UI", size=9, bold=True, color="FFFFFF")
    font_regular = Font(name="Segoe UI", size=9, color="2C3E50")
    font_bold = Font(name="Segoe UI", size=9, bold=True, color="2C3E50")
    font_link = Font(name="Segoe UI", size=9, color="1A5276", underline="single", bold=True)

    fill_main_banner = PatternFill(start_color="2C3E50", end_color="2C3E50", fill_type="solid")
    fill_zebra_light = PatternFill(start_color="F8F9FA", end_color="F8F9FA", fill_type="solid")
    
    fill_cat1 = PatternFill(start_color="D4EFDF", end_color="D4EFDF", fill_type="solid")
    fill_cat2 = PatternFill(start_color="E8F8F5", end_color="E8F8F5", fill_type="solid")
    fill_cat3 = PatternFill(start_color="FCF3CF", end_color="FCF3CF", fill_type="solid")
    fill_cat4 = PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid")

    border_light = Border(left=Side(style="thin", color="E5E7E9"), right=Side(style="thin", color="E5E7E9"),
                          top=Side(style="thin", color="E5E7E9"), bottom=Side(style="thin", color="E5E7E9"))

    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # Объединяем до колонки X (24-я колонка), так как добавилось новое поле
    ws.merge_cells("A1:X1")
    title_cell = ws["A1"]
    title_cell.value = "СВОДНЫЙ ЖУРНАЛ КОНТРОЛЯ КАЧЕСТВА И СТАТИСТИКИ ЗВОНКОВ"
    title_cell.font = font_title
    title_cell.fill = fill_main_banner
    title_cell.alignment = align_center
    ws.row_dimensions[1].height = 32

    # Добавлен столбец "Телефон клиента" 4-м по счету
    headers = [
        "ID Звонка", "Дата", "Время", "Телефон клиента", "Длительность", "Итого балл (/43)", 
        "Администратор", "Филиал клиники", 
        "Запись", "Дата приёма", "Время приёма", "Комментарий записи",
        "Контакт (/2)", "Имя (/2)", "Потребн. (/9)", "Презент. (/7)", "Цены (/3)", "Возраж. (/4)", 
        "К записи (/8)", "Финал (/3)", "Подход (/5)", "Резюме аудита", "Детальный отчёт", "Запись звонка"
    ]

    header_row = 3
    for col_idx, text in enumerate(headers, 1):
        cell = ws.cell(row=header_row, column=col_idx, value=text)
        cell.font = font_header
        cell.fill = PatternFill(start_color="34495E", end_color="34495E", fill_type="solid")
        cell.alignment = align_center
    ws.row_dimensions[header_row].height = 28

    current_row = header_row + 1
    criteria_keys = ["contact", "name_usage", "needs_analysis", "presentation",
                     "pricing", "objections", "closing_to_appointment", "termination", "individual_approach"]

    # Распаковываем кортеж из БД, где phone идет 4-м по счету аргументом (после duration)
    for row_idx, (start_time_str, call_id, duration, phone, admin_name, clinic_branch, analysis_text, record_url) in enumerate(calls_data):
        ws.row_dimensions[current_row].height = 24
        row_base_fill = fill_zebra_light if row_idx % 2 == 0 else None

        call_date = call_time = "-"
        if start_time_str:
            try:
                dt = datetime.strptime(start_time_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
                call_date = dt.strftime("%d.%m.%Y")
                call_time = dt.strftime("%H:%M")
            except:
                pass

        total_score = 0
        summary_text = "-"
        appointment_made = False
        app_date = app_time = app_comment = "-"
        scores_dict = {k: 0 for k in criteria_keys}

        if analysis_text:
            try:
                parsed = json.loads(analysis_text)
                total_score = int(parsed.get("total_score", 0))
                summary_text = parsed.get("summary", "-")
                admin_name = parsed.get("admin_name", admin_name)

                appointment_made = parsed.get("appointment_made", False)
                app_info = parsed.get("appointment_info", {})
                if isinstance(app_info, dict):
                    app_date = app_info.get("date", "-")
                    app_time = app_info.get("time", "-")
                    app_comment = app_info.get("comment", "-")

                details = parsed.get("details", {})
                for k in criteria_keys:
                    scores_dict[k] = details.get(k, {}).get("score", 0)
            except:
                pass

        # Формируем базовый блок данных (Телефон встает на 4-ю позицию, Длительность смещается на 5-ю)
        row_data = [
            str(call_id), call_date, call_time, str(phone or "-"), int(duration or 0), total_score,
            str(admin_name), str(clinic_branch),
            "Да" if appointment_made else "Нет", app_date, app_time, app_comment
        ]

        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=current_row, column=col_idx, value=value)
            cell.font = font_regular
            cell.border = border_light
            if row_base_fill: cell.fill = row_base_fill
            # Выравнивание по левому краю для Телефона (4), Админа (7), Клиники (8) и Комментария (12)
            cell.alignment = align_center if col_idx not in [4, 7, 8, 12] else align_left

        # Подсветка "Итого балл" (теперь колонка 6 вместо 5)
        score_cell = ws.cell(row=current_row, column=6)
        score_cell.font = font_bold
        if total_score >= 37: score_cell.fill = fill_cat1
        elif total_score >= 30: score_cell.fill = fill_cat2
        elif total_score >= 23: score_cell.fill = fill_cat3
        else: score_cell.fill = fill_cat4

        # Подсветка записи (теперь колонка 9 вместо 8)
        booking_cell = ws.cell(row=current_row, column=9)
        if appointment_made:
            booking_cell.fill = fill_cat1
            booking_cell.font = font_bold

        # Заполнение подкритериев оценок (колонки сдвинулись: старт с 13-й вместо 12-й)
        for idx, key in enumerate(criteria_keys, start=13):
            c_cell = ws.cell(row=current_row, column=idx, value=scores_dict[key])
            c_cell.alignment = align_center
            c_cell.font = font_regular
            c_cell.border = border_light
            if row_base_fill: c_cell.fill = row_base_fill
            if scores_dict[key] == 0:
                c_cell.font = font_bold
                c_cell.fill = fill_cat4

        # Резюме аудита (теперь колонка 22 вместо 21)
        sum_cell = ws.cell(row=current_row, column=22, value=summary_text)
        sum_cell.alignment = align_left
        sum_cell.font = font_regular
        sum_cell.border = border_light
        if row_base_fill: sum_cell.fill = row_base_fill

        # Детальный отчёт по ссылке (теперь колонка 23 вместо 22)
        real_public_url = public_urls_map.get(str(call_id))
        report_cell = ws.cell(row=current_row, column=23)
        
        if real_public_url:
            report_cell.value = "Открыть отчёт"
            report_cell.hyperlink = real_public_url
            report_cell.font = font_link
        else:
            report_cell.value = "Не синхронизирован"
            report_cell.font = font_regular
            
        report_cell.alignment = align_center
        report_cell.border = border_light
        if row_base_fill: report_cell.fill = row_base_fill

        # Ссылка на запись звонка (теперь колонка 24 вместо 23)
        link_cell = ws.cell(row=current_row, column=24, value="Слушать запись")
        if record_url:
            link_cell.hyperlink = record_url
            link_cell.font = font_link
        else:
            link_cell.value = "-"
            link_cell.font = font_regular
        link_cell.alignment = align_center
        link_cell.border = border_light
        if row_base_fill: link_cell.fill = row_base_fill

        current_row += 1

    # Обновленная сетка ширины колонок (добавлена колонка X, сдвинуты размеры)
    column_widths = {
        'A': 14, 'B': 13, 'C': 10, 'D': 18, 'E': 14, 'F': 17, 'G': 24, 'H': 24,
        'I': 11, 'J': 14, 'K': 14, 'L': 35,
        'M': 12, 'N': 12, 'O': 12, 'P': 12, 'Q': 12, 'R': 12, 'S': 12, 'T': 12, 'U': 12,
        'V': 60, 'W': 20, 'X': 18
    }
    for col_letter, width in column_widths.items():
        ws.column_dimensions[col_letter].width = width

    wb.save(MASTER_REPORT_PATH)
    return MASTER_REPORT_PATH