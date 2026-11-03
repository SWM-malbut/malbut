# Optional silent Piper work. This never admits or starts playback.
uint8 PREPARE=0
uint8 CANCEL=1
uint8 operation
string request_id
string generation_id
string prepared_id
# Exact decoded prefix of the eventual final text; empty for CANCEL.
string text
