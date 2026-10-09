"""Le fonti dei dati della lega: file caricati (``importers``, ``voti``,
``team_sheet_import``), API-Football (``apifootball``), ranking UEFA/FIFA
(``uefa``), classifiche da un link pubblico (``standings``).

Le rose, da qualunque fonte, arrivano a ``importers.import_rose_data`` in una
forma sola, una lista di squadre::

    {
        "name": "GELSI UNITED",
        "credits": 2168,            # crediti rimasti sulla fonte, o None
        "external_id": "1449",      # id della squadra sulla fonte, facoltativo
        "players": [
            {"role": "P", "name": "Falcone", "cost": 9, "club": "Lecce"},
            ...
        ],
    }
"""
