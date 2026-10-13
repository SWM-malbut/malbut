#include "homecam_media_agent/rotating_credentials.hpp"

#if HOMECAM_HAVE_KVS
#include <cstddef>
#include <limits>
#include <type_traits>

namespace homecam_media_agent
{

RotatingCredentials::RotatingCredentials()
: bridge_{{get}, this}
{
  static_assert(std::is_standard_layout_v<Bridge> && offsetof(Bridge, base) == 0);
}

RotatingCredentials::~RotatingCredentials()
{
  discard(&pending_);
  discard(&current_);
}

void RotatingCredentials::discard(PAwsCredentials * credentials)
{
  if (*credentials == nullptr) {return;}
  // createAwsCredentials allocates the structure and strings in one block.
  const UINT32 size = (*credentials)->size;
  volatile unsigned char * bytes = reinterpret_cast<volatile unsigned char *>(*credentials);
  for (UINT32 i = 0; i < size; ++i) {bytes[i] = 0;}
  freeAwsCredentials(credentials);
}

STATUS RotatingCredentials::stage(const SessionCredentials & credentials, std::int64_t now_ms)
{
  const auto valid_string = [](const std::string & value, std::size_t maximum) {
      return !value.empty() && value.size() <= maximum && value.find('\0') == std::string::npos;
    };
  if (now_ms < 0 || credentials.expires_at_unix_ms <= now_ms ||
    static_cast<UINT64>(credentials.expires_at_unix_ms) >
    std::numeric_limits<UINT64>::max() / HUNDREDS_OF_NANOS_IN_A_MILLISECOND ||
    !valid_string(credentials.access_key_id, MAX_ACCESS_KEY_LEN) ||
    !valid_string(credentials.secret_access_key, MAX_SECRET_KEY_LEN) ||
    !valid_string(credentials.session_token, MAX_SESSION_TOKEN_LEN))
  {
    return STATUS_INVALID_ARG;
  }
  const UINT64 expiry = static_cast<UINT64>(credentials.expires_at_unix_ms) *
    HUNDREDS_OF_NANOS_IN_A_MILLISECOND;
  std::lock_guard<std::mutex> lock(mutex_);
  const auto newest = pending_ != nullptr ? pending_ : current_;
  if (newest != nullptr && expiry <= newest->expiration) {return STATUS_INVALID_ARG;}
  PAwsCredentials next = nullptr;
  const STATUS result = createAwsCredentials(
    const_cast<char *>(credentials.access_key_id.c_str()), credentials.access_key_id.size(),
    const_cast<char *>(credentials.secret_access_key.c_str()), credentials.secret_access_key.size(),
    const_cast<char *>(credentials.session_token.c_str()), credentials.session_token.size(),
    expiry, &next);
  if (STATUS_FAILED(result)) {return result;}
  discard(&pending_);
  pending_ = next;
  return STATUS_SUCCESS;
}

STATUS RotatingCredentials::get(PAwsCredentialProvider provider, PAwsCredentials * credentials)
{
  if (provider == nullptr || credentials == nullptr) {return STATUS_NULL_ARG;}
  try {
    auto * self = reinterpret_cast<Bridge *>(provider)->owner;
    std::lock_guard<std::mutex> lock(self->mutex_);
    if (self->pending_ != nullptr) {
      discard(&self->current_);
      self->current_ = self->pending_;
      self->pending_ = nullptr;
    }
    *credentials = self->current_;
    return self->current_ != nullptr ? STATUS_SUCCESS : STATUS_INVALID_ARG;
  } catch (...) {
    *credentials = nullptr;
    return STATUS_INVALID_OPERATION;
  }
}

}  // namespace homecam_media_agent
#endif
