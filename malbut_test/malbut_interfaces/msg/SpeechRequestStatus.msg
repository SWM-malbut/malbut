# Lifecycle of an ordinary STT request; request_id is SpeechTranscript.utterance_id.
# This diagnostic contract carries no transcript and must never be spoken.
string ACCEPTED=accepted
string REJECTED=rejected
string FAILED=failed
string CANCELLED=cancelled
string request_id
string state
# Stable machine-readable reason; empty for normal acceptance.
string reason
