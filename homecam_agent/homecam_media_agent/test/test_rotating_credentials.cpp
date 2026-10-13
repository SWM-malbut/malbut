#include <gtest/gtest.h>

#include <atomic>
#include <limits>
#include <thread>

#include "homecam_media_agent/rotating_credentials.hpp"

using homecam_media_agent::RotatingCredentials;
using homecam_media_agent::SessionCredentials;

namespace
{
SessionCredentials credentials(std::int64_t expires, const char * token = "test-token")
{
  return {"test-access-key", "test-secret-key", token, expires};
}

STATUS read(RotatingCredentials & provider, PAwsCredentials * value)
{
  return provider.provider()->getCredentialsFn(provider.provider(), value);
}
}  // namespace

TEST(RotatingCredentials, EmptyAndExpiredInputsDoNotGrantCredentials)
{
  RotatingCredentials provider;
  PAwsCredentials value = nullptr;
  EXPECT_NE(read(provider, &value), STATUS_SUCCESS);
  EXPECT_EQ(value, nullptr);
  EXPECT_NE(provider.stage(credentials(100), 100), STATUS_SUCCESS);
  EXPECT_NE(provider.stage(SessionCredentials{}, 100), STATUS_SUCCESS);
  EXPECT_NE(provider.stage(credentials(std::numeric_limits<std::int64_t>::max()), 100), STATUS_SUCCESS);
  auto invalid = credentials(1000);
  invalid.session_token.assign("abc\0xyz", 7);
  EXPECT_NE(provider.stage(invalid, 100), STATUS_SUCCESS);
}

TEST(RotatingCredentials, StagingNeverMutatesOrFreesSdkBorrowedCredentials)
{
  RotatingCredentials provider;
  ASSERT_EQ(provider.stage(credentials(900'000, "first"), 0), STATUS_SUCCESS);
  PAwsCredentials first = nullptr;
  ASSERT_EQ(read(provider, &first), STATUS_SUCCESS);
  ASSERT_EQ(provider.stage(credentials(1'500'000, "second"), 600'000), STATUS_SUCCESS);
  EXPECT_STREQ(first->sessionToken, "first");
  EXPECT_EQ(first->expiration, 900'000U * HUNDREDS_OF_NANOS_IN_A_MILLISECOND);
  PAwsCredentials second = nullptr;
  ASSERT_EQ(read(provider, &second), STATUS_SUCCESS);
  EXPECT_STREQ(second->sessionToken, "second");
  EXPECT_EQ(second->expiration, 1'500'000U * HUNDREDS_OF_NANOS_IN_A_MILLISECOND);
  PAwsCredentials unchanged = nullptr;
  ASSERT_EQ(read(provider, &unchanged), STATUS_SUCCESS);
  EXPECT_EQ(unchanged, second);
}

TEST(RotatingCredentials, StaleOrInvalidRenewalKeepsLastSuccessfulDeadline)
{
  RotatingCredentials provider;
  ASSERT_EQ(provider.stage(credentials(900'000), 0), STATUS_SUCCESS);
  ASSERT_EQ(provider.stage(credentials(1'500'000), 600'000), STATUS_SUCCESS);
  EXPECT_NE(provider.stage(credentials(1'400'000), 600'000), STATUS_SUCCESS);
  EXPECT_NE(provider.stage(credentials(1'500'000), 600'000), STATUS_SUCCESS);
  PAwsCredentials value = nullptr;
  ASSERT_EQ(read(provider, &value), STATUS_SUCCESS);
  EXPECT_EQ(value->expiration, 1'500'000U * HUNDREDS_OF_NANOS_IN_A_MILLISECOND);
  // No refreshed credentials are fabricated when the accepted lease expires.
  EXPECT_EQ(read(provider, &value), STATUS_SUCCESS);
  EXPECT_LE(value->expiration, 1'500'000U * HUNDREDS_OF_NANOS_IN_A_MILLISECOND);
}

TEST(RotatingCredentials, ConcurrentPublisherLeavesBorrowedSnapshotValidUntilNextCallback)
{
  RotatingCredentials provider;
  ASSERT_EQ(provider.stage(credentials(900'000, "initial"), 0), STATUS_SUCCESS);
  PAwsCredentials value = nullptr;
  ASSERT_EQ(read(provider, &value), STATUS_SUCCESS);
  std::atomic<bool> done{false};
  std::atomic<int> failures{0};
  std::thread publisher([&]() {
      for (int i = 1; i <= 2000; ++i) {
        if (provider.stage(credentials(900'000 + i, "renewed"), 0) != STATUS_SUCCESS) {
          ++failures;
        }
      }
      done.store(true);
    });
  do {
    const auto expiry = value->expiration;
    const std::string token = value->sessionToken;
    std::this_thread::yield();
    EXPECT_EQ(value->expiration, expiry);
    EXPECT_EQ(std::string(value->sessionToken), token);
    EXPECT_EQ(read(provider, &value), STATUS_SUCCESS);
  } while (!done.load());
  publisher.join();
  EXPECT_EQ(failures.load(), 0);
  ASSERT_EQ(read(provider, &value), STATUS_SUCCESS);
  EXPECT_EQ(value->expiration, 902'000U * HUNDREDS_OF_NANOS_IN_A_MILLISECOND);
}

TEST(RotatingCredentials, FortyFiveMinutesCrossesThreeOldDeadlinesWithOneProvider)
{
  RotatingCredentials provider;
  const auto handle = provider.provider();
  auto expires = std::int64_t{900'000};
  ASSERT_EQ(provider.stage(credentials(expires), 0), STATUS_SUCCESS);
  PAwsCredentials value = nullptr;
  ASSERT_EQ(read(provider, &value), STATUS_SUCCESS);
  int renewals = 0;
  for (std::int64_t now = 0; now <= 45 * 60'000; now += 1000) {
    if (expires - now <= 300'000) {
      expires = now + 900'000;
      ASSERT_EQ(provider.stage(credentials(expires), now), STATUS_SUCCESS);
      ++renewals;
    }
    // Model the SDK asking for credentials when its borrowed value expires.
    if (value->expiration <= static_cast<UINT64>(now) * HUNDREDS_OF_NANOS_IN_A_MILLISECOND) {
      ASSERT_EQ(read(provider, &value), STATUS_SUCCESS);
    }
    ASSERT_GT(value->expiration, static_cast<UINT64>(now) * HUNDREDS_OF_NANOS_IN_A_MILLISECOND);
    ASSERT_EQ(provider.provider(), handle);
  }
  EXPECT_EQ(renewals, 4);
}
