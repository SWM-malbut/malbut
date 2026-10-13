#pragma once

#include "homecam_media_agent/build_features.hpp"

#if HOMECAM_HAVE_KVS
#include <mutex>

extern "C"
{
#include <com/amazonaws/kinesis/video/common/Include.h>
}

#include "homecam_media_agent/kvs_transport.hpp"

namespace homecam_media_agent
{

// One provider per signaling client. The network worker stages a prepared
// immutable snapshot; only the SDK getCredentials callback replaces the
// borrowed snapshot (the same lifetime contract as SDK v1.19.1's file/IoT
// providers). At most two snapshots are retained, and neither goes to disk.
// Destroy only after the signaling client and its workers have stopped.
class RotatingCredentials
{
public:
  RotatingCredentials();
  ~RotatingCredentials();
  RotatingCredentials(const RotatingCredentials &) = delete;
  RotatingCredentials & operator=(const RotatingCredentials &) = delete;
  PAwsCredentialProvider provider() {return &bridge_.base;}
  STATUS stage(const SessionCredentials & credentials, std::int64_t now_ms);

private:
  struct Bridge
  {
    AwsCredentialProvider base;
    RotatingCredentials * owner;
  };
  static STATUS get(PAwsCredentialProvider provider, PAwsCredentials * credentials);
  static void discard(PAwsCredentials * credentials);
  Bridge bridge_;
  std::mutex mutex_;
  PAwsCredentials current_{nullptr};
  PAwsCredentials pending_{nullptr};
};

}  // namespace homecam_media_agent
#endif
