#include "homecam_media_agent/http_runtime.hpp"

#include <mutex>

#include "homecam_media_agent/build_features.hpp"

#if HOMECAM_HAVE_CURL
#include <curl/curl.h>
#endif

namespace homecam_media_agent
{

bool ensure_http_runtime(std::string * const error)
{
#if HOMECAM_HAVE_CURL
  static std::once_flag initialize_once;
  static CURLcode initialize_result = CURLE_FAILED_INIT;
  std::call_once(
    initialize_once,
    []() {
      initialize_result = curl_global_init(CURL_GLOBAL_DEFAULT);
    });
  if (initialize_result != CURLE_OK) {
    if (error != nullptr) {
      *error =
        std::string("curl_global_init failed: ") +
        curl_easy_strerror(initialize_result);
    }
    return false;
  }
  return true;
#else
  if (error != nullptr) {
    *error = "homecam_media_agent was built without libcurl";
  }
  return false;
#endif
}

}  // namespace homecam_media_agent
