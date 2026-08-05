#pragma once

#include <string>

namespace homecam_media_agent
{

// Initializes libcurl exactly once before any asynchronous easy handle is
// created. curl_global_cleanup is intentionally left to process teardown.
bool ensure_http_runtime(std::string * error);

}  // namespace homecam_media_agent
