# Country catalog

`countries.json` contains the 245 country and territory choices served by the ZTB portal at `/assets/static/countries.json`, retrieved on 2026-09-25. The portal's country selector loads this asset directly. This is a snapshot of that portal's supported choices, not a generic list of every current ISO territory or a live API acceptance test.

- `name`: the portal's `backendName`, displayed in the editor.
- `value`: the portal's dictionary key / `name`, submitted for new ZIA locations through ZTB.
- `code`: the corresponding two-letter country code used for ZPA App Connector groups.
- `aliases`: alternate English names and common abbreviations accepted in CSV input.

Two-letter mappings and alternate names were cross-checked against [Unicode CLDR English territory names](https://github.com/unicode-org/cldr-json/blob/main/cldr-json/cldr-localenames-full/main/en/territories.json), with explicit mappings for vendor spellings. Legacy entries such as Netherlands Antilles (`AN`) and Swaziland are retained because they remain in the portal list; the catalog does not establish whether ZPA accepts every legacy territory.

To update the catalog, compare the portal asset with this file, preserve its exact API values, and review code mappings and aliases for additions or renames. Run `python3 -m unittest discover -s tests` and check both country inputs in the browser. The bundled list allows offline editing without making additional tenant requests.
