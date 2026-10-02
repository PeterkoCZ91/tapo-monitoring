"""User-facing texts of the labeling page and the stats views, one dictionary per language.

To add a language, copy ``en`` under a new code and translate the values; keep the
``{placeholders}``. Keys of every language must match (a test checks it). Keyboard keys,
label values (``person``, ``no_person``, ``unsure``) and JSON field names are not texts.
"""

from __future__ import annotations

DEFAULT_LANG = "en"

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # labeling page
        "page_title": "Frame labeling",
        "loading": "loading...",
        "stats_link": "stats",
        "img_alt": "frame",
        "camera": "camera",
        "source": "source",
        "person": "person",
        "teacher": "teacher",
        "btn_person": "&#9989; person",
        "btn_no_person": "&#10060; no person",
        "btn_unsure": "&#10067; unsure",
        "btn_skip": "skip",
        "btn_undo": "undo",
        "key_space": "space",
        "done": "Nothing left to label.",
        "undo_last": "undo last",
        "left": "left",
        "labeled_n": "labeled",
        "unknown": "unknown",
        "na": "n/a",
        "band_gray": "gray",
        "band_low": "low",
        "band_high": "high",
        "band_none": "none",
        # stats page chrome
        "stats_title": "Label stats",
        "back": "&larr; back to labeling",
        "stats_footnote": ("False alarms = sent frames labeled no_person. Misses = review "
                           "(held) frames labeled person. Unsure is excluded from both "
                           "rates."),
        "h_day_night": "Day and night",
        "h_incidents": "Incidents",
        "h_first_alert": "First alert by path",
        "no_incident_html": "No incident has a labeled frame yet.",
        # table headers
        "h_group": "group", "h_labeled": "labeled", "h_person": "person",
        "h_no_person": "no_person", "h_unsure": "unsure",
        "h_false_alarms_sent": "false alarms (sent)", "h_misses_review": "misses (review)",
        "h_slice": "slice", "h_n": "n", "h_best_threshold": "best threshold",
        "h_errors": "errors", "h_false_alarms": "false alarms", "h_misses": "misses",
        "h_note": "note", "h_incidents_col": "incidents", "h_person_alerted": "person alerted",
        "h_missed": "missed", "h_delay_median": "delay median", "h_delay_p90": "delay p90",
        "h_path": "path", "h_first_alerts": "first alerts", "h_share": "share",
        "h_delay_n": "delay n",
        # row names
        "r_all": "all", "r_band": "band {name}", "r_camera": "camera {name}",
        "r_camera_part": "camera {cam} {name}", "d_day": "day", "d_night": "night",
        "d_unknown": "unknown",
        # sentences
        "too_few": "too few labels",
        "threshold_na": "best threshold: n/a (no decided frames with a score)",
        "threshold_line": ("best threshold: person >= {threshold:.2f} -> {errors} errors of "
                           "{n} ({false_alarms} false alarms, {misses} misses, accuracy "
                           "{accuracy})"),
        "verdict_head": ("misses by review verdict (person in a held frame = hold error, in "
                         "a dropped frame = real miss):"),
        "verdict_sampled": (", {sampled} of them sampled -> estimated {est_person:.0f} "
                            "person of {est_decided:.0f} frames (count / sample_rate; an "
                            "estimate)"),
        "day_night_summary": ("day/night of the labeled frames ({counts}; night as the "
                              "daemon judged it, camera schedules applied)"),
        "day_night_note": ("{too_few} = fewer than {decided} decided frames with a score or "
                           "fewer than {per_class} of either class: noise, not a threshold "
                           "to ship"),
        "incident_summary": ("incidents: {total} from {frames} frames (one incident per "
                             "incident ID, else frames of a camera less than {gap:.0f} s "
                             "apart), {labeled} with a labeled frame"),
        "incident_note_1": ("person = any frame labeled person; missed = person incident "
                            "without a delivered alert; false alarms = alerted incidents "
                            "whose labeled frames are all no_person, of alerted incidents "
                            "with a decided label"),
        "incident_note_delay": ("delay = event start to the first delivered alert, over {n} "
                                "alerted person incidents ({timed} timed from the camera's "
                                "event start or its time in the caption, the rest from "
                                "their first archived frame)"),
        "missed_none": "no review frame {incidents}",
        "missed_verdict": "{name} {incidents} (person labeled in {person})",
        "missed_head": "missed person incidents by the verdicts of their review frames: ",
        "first_alert_summary": ("first alert by delivery path (every alerted incident, "
                                "labeled or not; delay = event start to the first delivered "
                                "alert, sent by that path; unknown = a sent-log record from "
                                "before the path was recorded)"),
        "no_incident_text": ("no incident has a labeled frame yet: label frames to measure "
                             "missed people per incident"),
        "labels_by": "labels: {human} by a person, {auto} automatic (both models agreed)",
        "stats_footnote_text": ("false alarms = sent frames labeled no_person; misses = "
                                "review (held) frames labeled person; unsure excluded"),
    },
    "cs": {
        "page_title": "Štítkování snímků",
        "loading": "načítám...",
        "stats_link": "statistiky",
        "img_alt": "snímek",
        "camera": "kamera",
        "source": "zdroj",
        "person": "osoba",
        "teacher": "učitel",
        "btn_person": "&#9989; osoba",
        "btn_no_person": "&#10060; žádná osoba",
        "btn_unsure": "&#10067; nejisté",
        "btn_skip": "přeskočit",
        "btn_undo": "zpět",
        "key_space": "mezerník",
        "done": "Nezbývá nic k označení.",
        "undo_last": "vrátit poslední",
        "left": "zbývá",
        "labeled_n": "označeno",
        "unknown": "neznámá",
        "na": "n/a",
        "band_gray": "šedá zóna",
        "band_low": "nízké",
        "band_high": "vysoké",
        "band_none": "bez skóre",
        "stats_title": "Statistiky štítků",
        "back": "&larr; zpět na štítkování",
        "stats_footnote": ("Falešné poplachy = odeslané snímky označené jako žádná osoba. "
                           "Zmeškané = zadržené (review) snímky označené jako osoba. "
                           "Nejisté se do obou měr nepočítají."),
        "h_day_night": "Den a noc",
        "h_incidents": "Incidenty",
        "h_first_alert": "První alert podle cesty",
        "no_incident_html": "Zatím žádný incident nemá označený snímek.",
        "h_group": "skupina", "h_labeled": "označeno", "h_person": "osoba",
        "h_no_person": "žádná osoba", "h_unsure": "nejisté",
        "h_false_alarms_sent": "falešné poplachy (odeslané)",
        "h_misses_review": "zmeškané (review)",
        "h_slice": "řez", "h_n": "n", "h_best_threshold": "nejlepší práh",
        "h_errors": "chyby", "h_false_alarms": "falešné poplachy", "h_misses": "zmeškané",
        "h_note": "poznámka", "h_incidents_col": "incidenty",
        "h_person_alerted": "osoba oznámena", "h_missed": "zmeškané",
        "h_delay_median": "zpoždění medián", "h_delay_p90": "zpoždění p90",
        "h_path": "cesta", "h_first_alerts": "první alerty", "h_share": "podíl",
        "h_delay_n": "zpoždění n",
        "r_all": "vše", "r_band": "pásmo {name}", "r_camera": "kamera {name}",
        "r_camera_part": "kamera {cam} {name}", "d_day": "den", "d_night": "noc",
        "d_unknown": "neznámé",
        "too_few": "příliš málo štítků",
        "threshold_na": "nejlepší práh: n/a (žádné rozhodnuté snímky se skóre)",
        "threshold_line": ("nejlepší práh: osoba >= {threshold:.2f} -> {errors} chyb z {n} "
                           "({false_alarms} falešných poplachů, {misses} zmeškaných, "
                           "přesnost {accuracy})"),
        "verdict_head": ("zmeškané podle verdiktu review (osoba v zadrženém snímku = chyba "
                         "zadržení, ve zahozeném snímku = skutečné zmeškání):"),
        "verdict_sampled": (", z toho {sampled} vzorkovaných -> odhadem {est_person:.0f} "
                            "osob z {est_decided:.0f} snímků (počet / sample_rate; jen "
                            "odhad)"),
        "day_night_summary": ("den/noc označených snímků ({counts}; noc tak, jak ji posoudil "
                              "démon, s rozvrhy kamer)"),
        "day_night_note": ("{too_few} = méně než {decided} rozhodnutých snímků se skóre "
                           "nebo méně než {per_class} snímků některé třídy: šum, ne práh "
                           "k nasazení"),
        "incident_summary": ("incidenty: {total} z {frames} snímků (jeden incident na ID "
                             "incidentu, jinak snímky kamery vzdálené méně než {gap:.0f} s), "
                             "{labeled} s označeným snímkem"),
        "incident_note_1": ("osoba = jakýkoli snímek označený jako osoba; zmeškané = "
                            "incident s osobou bez doručeného alertu; falešné poplachy = "
                            "oznámené incidenty, jejichž označené snímky jsou všechny bez "
                            "osoby, z oznámených incidentů s rozhodnutým štítkem"),
        "incident_note_delay": ("zpoždění = od začátku události do prvního doručeného "
                                "alertu, přes {n} oznámených incidentů s osobou ({timed} "
                                "měřeno od začátku události z kamery nebo od času v popisku, "
                                "zbytek od prvního archivovaného snímku)"),
        "missed_none": "bez review snímku {incidents}",
        "missed_verdict": "{name} {incidents} (osoba označena v {person})",
        "missed_head": "zmeškané incidenty s osobou podle verdiktů jejich review snímků: ",
        "first_alert_summary": ("první alert podle cesty doručení (každý oznámený incident, "
                                "označený či ne; zpoždění = od začátku události do prvního "
                                "doručeného alertu odeslaného touto cestou; unknown = záznam "
                                "sent-logu z doby před zaznamenáváním cesty)"),
        "no_incident_text": ("zatím žádný incident nemá označený snímek: označte snímky, "
                             "abyste mohli měřit zmeškané osoby podle incidentu"),
        "labels_by": "štítky: {human} od člověka, {auto} automatických (oba modely se shodly)",
        "stats_footnote_text": ("falešné poplachy = odeslané snímky označené jako žádná "
                                "osoba; zmeškané = zadržené (review) snímky označené jako "
                                "osoba; nejisté vynecháno"),
    },
}


def tr(lang, key, **kwargs):
    """Text ``key`` in ``lang`` (English when the language or key is missing)."""
    table = STRINGS.get(lang) or STRINGS[DEFAULT_LANG]
    text = table.get(key)
    if text is None:
        text = STRINGS[DEFAULT_LANG][key]
    return text.format(**kwargs) if kwargs else text
