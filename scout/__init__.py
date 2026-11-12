"""Scout: scopre e tiene aggiornato il registro delle risorse di .brAIan.

Scout conosce le risorse (modelli, servizi, endpoint). Non decide, non
conversa, non legge la memoria, non esegue inferenze: la scelta finale
spetta a System One.
"""

import logging

# Usato come libreria, Scout non scrive niente a schermo: i log vanno su file
# solo quando la CLI chiama config.setup_logging().
logging.getLogger("scout").addHandler(logging.NullHandler())

__version__ = "0.1.0"
SCHEMA_VERSION = "1.0.0"
