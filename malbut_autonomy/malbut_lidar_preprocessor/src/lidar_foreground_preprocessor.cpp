#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <tf2/exceptions.h>
#include <tf2_ros/buffer.h>
#include <tf2_ros/transform_listener.h>

#include <laser_geometry/laser_geometry.hpp>
#include <malbut_interfaces/msg/lidar_cluster.hpp>
#include <malbut_interfaces/msg/lidar_cluster_array.hpp>
#include <nav_msgs/msg/occupancy_grid.hpp>
#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>
#include <sensor_msgs/msg/point_cloud2.hpp>
#include <sensor_msgs/msg/point_field.hpp>

namespace
{
constexpr double kTau = 6.28318530717958647692;

struct PointWithIndex
{
  float x;
  float y;
  std::uint32_t scan_index;
};

struct PointGroup
{
  std::vector<PointWithIndex> points;
};

double planar_distance(const PointWithIndex & left, const PointWithIndex & right)
{
  return std::hypot(
    static_cast<double>(left.x - right.x),
    static_cast<double>(left.y - right.y));
}

const sensor_msgs::msg::PointField * find_field(
  const sensor_msgs::msg::PointCloud2 & cloud,
  const std::string & name)
{
  const auto found = std::find_if(
    cloud.fields.begin(), cloud.fields.end(),
    [&name](const sensor_msgs::msg::PointField & field) {
      return field.name == name;
    });
  return found == cloud.fields.end() ? nullptr : &*found;
}

template<typename T>
T read_value(const std::uint8_t * point, std::uint32_t offset)
{
  T value{};
  std::memcpy(&value, point + offset, sizeof(T));
  return value;
}

std::uint32_t read_scan_index(
  const std::uint8_t * point,
  const sensor_msgs::msg::PointField & field)
{
  using sensor_msgs::msg::PointField;
  switch (field.datatype) {
    case PointField::INT32:
      return static_cast<std::uint32_t>(read_value<std::int32_t>(point, field.offset));
    case PointField::UINT32:
      return read_value<std::uint32_t>(point, field.offset);
    case PointField::FLOAT32:
      return static_cast<std::uint32_t>(read_value<float>(point, field.offset));
    default:
      throw std::runtime_error("unsupported laser_geometry index datatype");
  }
}
}  // namespace

class LidarForegroundPreprocessor : public rclcpp::Node
{
public:
  LidarForegroundPreprocessor()
  : Node("lidar_foreground_preprocessor"),
    tf_buffer_(std::make_unique<tf2_ros::Buffer>(get_clock())),
    tf_listener_(std::make_shared<tf2_ros::TransformListener>(*tf_buffer_))
  {
    declare_parameters();
    read_and_validate_parameters();

    clusters_publisher_ = create_publisher<malbut_interfaces::msg::LidarClusterArray>(
      clusters_topic_, rclcpp::SensorDataQoS());
    static_map_subscription_ = create_subscription<nav_msgs::msg::OccupancyGrid>(
      static_map_topic_,
      rclcpp::QoS(1).transient_local().reliable(),
      std::bind(&LidarForegroundPreprocessor::on_static_map, this, std::placeholders::_1));
    scan_subscription_ = create_subscription<sensor_msgs::msg::LaserScan>(
      scan_topic_, rclcpp::SensorDataQoS(),
      std::bind(&LidarForegroundPreprocessor::queue_scan, this, std::placeholders::_1));
    scan_transform_timer_ = create_wall_timer(
      std::chrono::milliseconds(20),
      std::bind(&LidarForegroundPreprocessor::process_pending_scan, this));

    RCLCPP_INFO(
      get_logger(),
      "LiDAR foreground preprocessor ready: %s -> %s (%s frame)",
      scan_topic_.c_str(), clusters_topic_.c_str(), global_frame_.c_str());
  }

private:
  void declare_parameters()
  {
    declare_parameter("scan_topic", "/scan");
    declare_parameter("static_map_topic", "/map");
    declare_parameter("clusters_topic", "/perception/lidar/foreground_clusters");
    declare_parameter("global_frame", "map");
    declare_parameter("static_occupied_threshold", 65);
    declare_parameter("static_exclusion_radius_m", 0.20);
    declare_parameter("cluster_gap_m", 0.20);
    declare_parameter("minimum_cluster_points", 3);
    declare_parameter("minimum_cluster_density_points_per_m", 5.0);
    declare_parameter("maximum_cluster_points", 120);
    declare_parameter("maximum_cluster_extent_m", 0.80);
    declare_parameter("sensor_transform_queue_timeout_s", 0.30);
  }

  void read_and_validate_parameters()
  {
    scan_topic_ = get_parameter("scan_topic").as_string();
    static_map_topic_ = get_parameter("static_map_topic").as_string();
    clusters_topic_ = get_parameter("clusters_topic").as_string();
    global_frame_ = get_parameter("global_frame").as_string();
    occupied_threshold_ = get_parameter("static_occupied_threshold").as_int();
    exclusion_radius_m_ = get_parameter("static_exclusion_radius_m").as_double();
    cluster_gap_m_ = get_parameter("cluster_gap_m").as_double();
    minimum_cluster_points_ = get_parameter("minimum_cluster_points").as_int();
    minimum_density_ =
      get_parameter("minimum_cluster_density_points_per_m").as_double();
    maximum_cluster_points_ = get_parameter("maximum_cluster_points").as_int();
    maximum_cluster_extent_m_ =
      get_parameter("maximum_cluster_extent_m").as_double();
    transform_queue_timeout_s_ =
      get_parameter("sensor_transform_queue_timeout_s").as_double();

    if (scan_topic_.empty() || scan_topic_.front() != '/' ||
      static_map_topic_.empty() || static_map_topic_.front() != '/' ||
      clusters_topic_.empty() || clusters_topic_.front() != '/')
    {
      throw std::invalid_argument("sensor and output topics must be absolute");
    }
    if (global_frame_.empty()) {
      throw std::invalid_argument("global_frame must not be empty");
    }
    if (occupied_threshold_ < 0 || occupied_threshold_ > 100) {
      throw std::invalid_argument("static_occupied_threshold must be in [0, 100]");
    }
    if (exclusion_radius_m_ < 0.0 || cluster_gap_m_ <= 0.0 ||
      minimum_cluster_points_ <= 0 || minimum_density_ <= 0.0 ||
      maximum_cluster_points_ < minimum_cluster_points_ ||
      maximum_cluster_extent_m_ <= 0.0 || transform_queue_timeout_s_ <= 0.0)
    {
      throw std::invalid_argument("invalid foreground clustering parameters");
    }
  }

  void on_static_map(const nav_msgs::msg::OccupancyGrid::SharedPtr message)
  {
    const auto expected_size =
      static_cast<std::size_t>(message->info.width) * message->info.height;
    if (message->header.frame_id != global_frame_ || message->info.resolution <= 0.0 ||
      message->info.width == 0U || message->info.height == 0U ||
      message->data.size() != expected_size)
    {
      RCLCPP_WARN(
        get_logger(),
        "Ignoring invalid static map or frame mismatch (%s != %s)",
        message->header.frame_id.c_str(), global_frame_.c_str());
      return;
    }

    map_width_ = message->info.width;
    map_height_ = message->info.height;
    map_resolution_ = message->info.resolution;
    map_origin_x_ = message->info.origin.position.x;
    map_origin_y_ = message->info.origin.position.y;
    const auto & orientation = message->info.origin.orientation;
    map_origin_yaw_ = std::atan2(
      2.0 * (orientation.w * orientation.z + orientation.x * orientation.y),
      1.0 - 2.0 *
      (orientation.y * orientation.y + orientation.z * orientation.z));
    map_origin_cos_ = std::cos(map_origin_yaw_);
    map_origin_sin_ = std::sin(map_origin_yaw_);

    known_.assign(expected_size, false);
    cv::Mat free_mask(
      static_cast<int>(map_height_), static_cast<int>(map_width_), CV_8UC1,
      cv::Scalar(255));
    bool has_occupied_cell = false;
    for (std::size_t index = 0; index < expected_size; ++index) {
      const auto value = static_cast<int>(message->data[index]);
      known_[index] = value >= 0;
      if (value >= occupied_threshold_) {
        free_mask.at<std::uint8_t>(
          static_cast<int>(index / map_width_),
          static_cast<int>(index % map_width_)) = 0;
        has_occupied_cell = true;
      }
    }

    if (has_occupied_cell) {
      cv::distanceTransform(free_mask, static_distance_m_, cv::DIST_L2, 5);
      static_distance_m_ *= static_cast<float>(map_resolution_);
    } else {
      static_distance_m_ = cv::Mat(
        static_cast<int>(map_height_), static_cast<int>(map_width_), CV_32FC1,
        cv::Scalar(std::numeric_limits<float>::infinity()));
    }
    map_ready_ = true;
    RCLCPP_INFO(
      get_logger(), "Cached static distance field (%u x %u)",
      map_width_, map_height_);
  }

  std::optional<float> static_clearance(float world_x, float world_y) const
  {
    const double offset_x = static_cast<double>(world_x) - map_origin_x_;
    const double offset_y = static_cast<double>(world_y) - map_origin_y_;
    const double local_x = map_origin_cos_ * offset_x + map_origin_sin_ * offset_y;
    const double local_y = -map_origin_sin_ * offset_x + map_origin_cos_ * offset_y;
    const auto cell_x = static_cast<std::int64_t>(std::floor(local_x / map_resolution_));
    const auto cell_y = static_cast<std::int64_t>(std::floor(local_y / map_resolution_));
    if (cell_x < 0 || cell_y < 0 ||
      cell_x >= static_cast<std::int64_t>(map_width_) ||
      cell_y >= static_cast<std::int64_t>(map_height_))
    {
      return std::nullopt;
    }
    const auto index = static_cast<std::size_t>(cell_y) * map_width_ + cell_x;
    if (!known_[index]) {
      return std::nullopt;
    }
    return static_distance_m_.at<float>(
      static_cast<int>(cell_y), static_cast<int>(cell_x));
  }

  void queue_scan(const sensor_msgs::msg::LaserScan::ConstSharedPtr scan)
  {
    if (!map_ready_ || scan->header.frame_id.empty()) {
      return;
    }
    // Match the previous follower semantics: retain only the freshest scan and
    // retry it briefly while its measurement-time TF catches up.
    pending_scan_ = scan;
    process_pending_scan();
  }

  void process_pending_scan()
  {
    const auto scan = pending_scan_;
    if (scan == nullptr) {
      return;
    }

    sensor_msgs::msg::PointCloud2 cloud;
    try {
      projector_.transformLaserScanToPointCloud(
        global_frame_, *scan, cloud, *tf_buffer_, -1.0,
        laser_geometry::channel_option::Index);
    } catch (const tf2::TransformException & error) {
      const rclcpp::Time stamp(scan->header.stamp, get_clock()->get_clock_type());
      const double age_s = (get_clock()->now() - stamp).seconds();
      if (age_s > transform_queue_timeout_s_) {
        pending_scan_.reset();
        RCLCPP_WARN_THROTTLE(
          get_logger(), *get_clock(), 5000,
          "Dropping scan without measurement-time TF after %.2f s: %s",
          transform_queue_timeout_s_, error.what());
      }
      return;
    }
    pending_scan_.reset();

    const auto * x_field = find_field(cloud, "x");
    const auto * y_field = find_field(cloud, "y");
    const auto * index_field = find_field(cloud, "index");
    if (x_field == nullptr || y_field == nullptr || index_field == nullptr ||
      x_field->datatype != sensor_msgs::msg::PointField::FLOAT32 ||
      y_field->datatype != sensor_msgs::msg::PointField::FLOAT32)
    {
      RCLCPP_ERROR_THROTTLE(
        get_logger(), *get_clock(), 5000,
        "laser_geometry output is missing x, y, or index fields");
      return;
    }

    std::vector<PointGroup> groups;
    PointGroup current;
    const auto point_count = static_cast<std::size_t>(cloud.width) * cloud.height;
    for (std::size_t cloud_index = 0; cloud_index < point_count; ++cloud_index) {
      const auto * bytes = cloud.data.data() + cloud_index * cloud.point_step;
      PointWithIndex point{
        read_value<float>(bytes, x_field->offset),
        read_value<float>(bytes, y_field->offset),
        read_scan_index(bytes, *index_field)};
      const auto clearance = static_clearance(point.x, point.y);
      if (!clearance.has_value() || *clearance <= exclusion_radius_m_) {
        if (!current.points.empty()) {
          groups.push_back(std::move(current));
          current = PointGroup{};
        }
        continue;
      }
      const bool contiguous = current.points.empty() ||
        (point.scan_index == current.points.back().scan_index + 1U &&
        planar_distance(point, current.points.back()) <= cluster_gap_m_);
      if (!contiguous && !current.points.empty()) {
        groups.push_back(std::move(current));
        current = PointGroup{};
      }
      current.points.push_back(point);
    }
    if (!current.points.empty()) {
      groups.push_back(std::move(current));
    }

    const double covered_angle = std::abs(scan->angle_increment) *
      static_cast<double>(scan->ranges.empty() ? 0U : scan->ranges.size() - 1U);
    if (groups.size() > 1U && covered_angle >= kTau - 2.0 * std::abs(scan->angle_increment) &&
      groups.front().points.front().scan_index == 0U &&
      groups.back().points.back().scan_index + 1U == scan->ranges.size() &&
      planar_distance(groups.back().points.back(), groups.front().points.front()) <= cluster_gap_m_)
    {
      auto merged = std::move(groups.back().points);
      merged.insert(
        merged.end(), groups.front().points.begin(), groups.front().points.end());
      groups.front().points = std::move(merged);
      groups.pop_back();
    }

    malbut_interfaces::msg::LidarClusterArray output;
    output.header = scan->header;
    output.header.frame_id = global_frame_;
    for (const auto & group : groups) {
      append_cluster(group, output);
    }
    clusters_publisher_->publish(output);
  }

  void append_cluster(
    const PointGroup & group,
    malbut_interfaces::msg::LidarClusterArray & output) const
  {
    const auto count = static_cast<std::int64_t>(group.points.size());
    if (count < minimum_cluster_points_ || count > maximum_cluster_points_) {
      return;
    }
    float minimum_x = group.points.front().x;
    float maximum_x = minimum_x;
    float minimum_y = group.points.front().y;
    float maximum_y = minimum_y;
    double sum_x = 0.0;
    double sum_y = 0.0;
    for (const auto & point : group.points) {
      minimum_x = std::min(minimum_x, point.x);
      maximum_x = std::max(maximum_x, point.x);
      minimum_y = std::min(minimum_y, point.y);
      maximum_y = std::max(maximum_y, point.y);
      sum_x += point.x;
      sum_y += point.y;
    }
    const double extent = std::hypot(maximum_x - minimum_x, maximum_y - minimum_y);
    const double density = static_cast<double>(count) / std::max(extent, 0.05);
    if (extent > maximum_cluster_extent_m_ || density < minimum_density_) {
      return;
    }
    malbut_interfaces::msg::LidarCluster cluster;
    cluster.position.x = sum_x / static_cast<double>(count);
    cluster.position.y = sum_y / static_cast<double>(count);
    cluster.position.z = 0.0;
    cluster.point_count = static_cast<std::uint32_t>(count);
    cluster.extent_m = static_cast<float>(extent);
    output.clusters.push_back(cluster);
  }

  std::string scan_topic_;
  std::string static_map_topic_;
  std::string clusters_topic_;
  std::string global_frame_;
  std::int64_t occupied_threshold_{65};
  double exclusion_radius_m_{0.20};
  double cluster_gap_m_{0.20};
  std::int64_t minimum_cluster_points_{3};
  double minimum_density_{5.0};
  std::int64_t maximum_cluster_points_{120};
  double maximum_cluster_extent_m_{0.80};
  double transform_queue_timeout_s_{0.30};

  bool map_ready_{false};
  std::uint32_t map_width_{0U};
  std::uint32_t map_height_{0U};
  double map_resolution_{0.0};
  double map_origin_x_{0.0};
  double map_origin_y_{0.0};
  double map_origin_yaw_{0.0};
  double map_origin_cos_{1.0};
  double map_origin_sin_{0.0};
  std::vector<bool> known_;
  cv::Mat static_distance_m_;

  laser_geometry::LaserProjection projector_;
  std::unique_ptr<tf2_ros::Buffer> tf_buffer_;
  std::shared_ptr<tf2_ros::TransformListener> tf_listener_;
  rclcpp::Subscription<nav_msgs::msg::OccupancyGrid>::SharedPtr static_map_subscription_;
  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr scan_subscription_;
  rclcpp::TimerBase::SharedPtr scan_transform_timer_;
  sensor_msgs::msg::LaserScan::ConstSharedPtr pending_scan_;
  rclcpp::Publisher<malbut_interfaces::msg::LidarClusterArray>::SharedPtr
    clusters_publisher_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<LidarForegroundPreprocessor>());
  rclcpp::shutdown();
  return 0;
}
