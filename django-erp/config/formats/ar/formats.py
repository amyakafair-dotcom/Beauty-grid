# Sudan writes numbers as 1,250.50 with Latin digits; Django's Arabic default uses a comma decimal.
from django.conf.locale.ar.formats import *  # noqa: F401,F403

DECIMAL_SEPARATOR = "."
THOUSAND_SEPARATOR = ","
NUMBER_GROUPING = 3
