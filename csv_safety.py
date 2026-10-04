"""CSV configuration exports: preserve plain text, reject formula-like cells."""
import csv
import io
import unicodedata

from credential_fields import clean_values, is_credential_field


def check_cell(value):
    text = unicodedata.normalize('NFKC', str(value if value is not None else ''))
    # Spreadsheet importers can ignore whitespace and invisible leading marks.
    while text and (text[0].isspace() or unicodedata.category(text[0]) == 'Cf'):
        text = text[1:]
    if text.startswith(('=', '+', '-', '@')):
        raise ValueError('CSV export blocked: a value or column starts with a spreadsheet '
                         'formula character (=, +, -, or @). Change it to plain text before exporting.')


def csv_text(rows, defaults):
    rows = clean_values(list(rows))
    defaults = [key for key in defaults if not is_credential_field(key)]
    headers = list(dict.fromkeys([*defaults, *(key for row in rows for key in row)]))
    for header in headers:
        check_cell(header)
    for row in rows:
        for value in row.values():
            check_cell(value)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=headers, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()
