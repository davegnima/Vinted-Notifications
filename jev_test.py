# Test di Jev (TypeSafe AI) sui titoli degli annunci del radar senza modello (25/10).
# Parte solo con JEV_TEST=1: scrive le risposte nei log e poi lascia il servizio fermo (nessuno scraping).
# La chiave viene da TYPESAFE_API_KEY e non viene mai scritta nei log.
import json
import os
import urllib.request

from logger import get_logger

logger = get_logger(__name__)

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODELLO = "jev-latest"
FILE_TITOLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jev_test", "titoli.json")


def chiedi(chiave, annuncio):
    opzioni = annuncio["candidati"] + ["nessuno"]
    corpo = {
        "model": MODELLO,
        "state": f"Titolo annuncio Vinted: {annuncio['titolo']}\nPrezzo: {annuncio['prezzo']} EUR",
        "questions": {"modello": {
            "type": "choice",
            "instructions": "Quale modello descrive il titolo? Scegli tra le opzioni. "
                            "Se il titolo non basta per riconoscerne uno, scegli 'nessuno'.",
            "criteria": {o: f"il modello e' {o}" for o in opzioni},
        }},
    }
    req = urllib.request.Request(ENDPOINT, data=json.dumps(corpo).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {chiave}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def esegui():
    chiave = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not chiave:
        logger.error("JEV TEST: manca TYPESAFE_API_KEY")
        return
    annunci = json.load(open(FILE_TITOLI))
    logger.info("JEV TEST | avvio su %d titoli", len(annunci))
    for a in annunci:
        try:
            esito = chiedi(chiave, a)
            logger.info("JEV TEST | item=%s | prezzo=%s | titolo=%s | risposta=%s",
                        a["id"], a["prezzo"], a["titolo"][:70], json.dumps(esito, ensure_ascii=False)[:400])
        except Exception as e:
            logger.error("JEV TEST | item=%s | errore=%s", a["id"], str(e)[:300])
    logger.info("JEV TEST | fine. Servizio fermo: togliere JEV_TEST per riprendere il radar.")
