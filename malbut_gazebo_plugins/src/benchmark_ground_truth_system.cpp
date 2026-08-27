#include <chrono>
#include <memory>
#include <stdexcept>
#include <string>

#include <ignition/gazebo/Actor.hh>
#include <ignition/gazebo/EntityComponentManager.hh>
#include <ignition/gazebo/System.hh>
#include <ignition/gazebo/components/Actor.hh>
#include <ignition/gazebo/components/Model.hh>
#include <ignition/gazebo/components/Name.hh>
#include <ignition/gazebo/components/Pose.hh>
#include <ignition/msgs/pose_v.pb.h>
#include <ignition/plugin/Register.hh>
#include <ignition/transport/Node.hh>
#include <sdf/Element.hh>

namespace malbut::gazebo
{
class BenchmarkGroundTruthSystem final:
  public ignition::gazebo::System,
  public ignition::gazebo::ISystemConfigure,
  public ignition::gazebo::ISystemPostUpdate
{
public:
  void Configure(
    const ignition::gazebo::Entity &,
    const std::shared_ptr<const sdf::Element> &_sdf,
    ignition::gazebo::EntityComponentManager &,
    ignition::gazebo::EventManager &) override
  {
    this->robotName = _sdf->Get<std::string>(
      "robot_name", "malbut").first;
    this->actorName = _sdf->Get<std::string>(
      "actor_name", "benchmark_person").first;
    const auto topic = _sdf->Get<std::string>(
      "topic", "/benchmark/ground_truth").first;
    const auto publishRate = _sdf->Get<double>(
      "publish_rate", 20.0).first;
    if (publishRate <= 0.0)
      throw std::runtime_error("benchmark publish rate must be positive");
    this->publishPeriod = std::chrono::duration_cast<
      std::chrono::steady_clock::duration>(
      std::chrono::duration<double>(1.0 / publishRate));
    this->publisher = this->node.Advertise<ignition::msgs::Pose_V>(topic);
  }

  void PostUpdate(
    const ignition::gazebo::UpdateInfo &_info,
    const ignition::gazebo::EntityComponentManager &_ecm) override
  {
    if (_info.paused || _info.simTime < this->nextPublication)
      return;
    this->nextPublication = _info.simTime + this->publishPeriod;

    ignition::msgs::Pose_V message;
    const auto nanoseconds = std::chrono::duration_cast<
      std::chrono::nanoseconds>(_info.simTime).count();
    auto *stamp = message.mutable_header()->mutable_stamp();
    stamp->set_sec(nanoseconds / 1000000000LL);
    stamp->set_nsec(nanoseconds % 1000000000LL);
    this->AppendRobotPose(_ecm, message);
    this->AppendActorPose(_ecm, message);
    if (message.pose_size() == 2)
      this->publisher.Publish(message);
  }

private:
  void AppendRobotPose(
    const ignition::gazebo::EntityComponentManager &_ecm,
    ignition::msgs::Pose_V &_message) const
  {
    _ecm.Each<ignition::gazebo::components::Model,
      ignition::gazebo::components::Name,
      ignition::gazebo::components::Pose>(
      [&](const ignition::gazebo::Entity &,
          const ignition::gazebo::components::Model *,
          const ignition::gazebo::components::Name *_name,
          const ignition::gazebo::components::Pose *_pose)
      {
        if (_name->Data() != this->robotName)
          return true;
        this->AppendPose(this->robotName, _pose->Data(), _message);
        return false;
      });
  }

  void AppendActorPose(
    const ignition::gazebo::EntityComponentManager &_ecm,
    ignition::msgs::Pose_V &_message)
  {
    _ecm.Each<ignition::gazebo::components::Actor,
      ignition::gazebo::components::Name>(
      [&](const ignition::gazebo::Entity &_entity,
          const ignition::gazebo::components::Actor *,
          const ignition::gazebo::components::Name *_name)
      {
        if (_name->Data() != this->actorName)
          return true;
        const ignition::gazebo::Actor actor(_entity);
        const auto worldPose = actor.WorldPose(_ecm);
        if (worldPose.has_value())
          this->AppendPose(this->actorName, *worldPose, _message);
        return false;
      });
  }

  void AppendPose(
    const std::string &_name,
    const ignition::math::Pose3d &_source,
    ignition::msgs::Pose_V &_message) const
  {
    auto *pose = _message.add_pose();
    pose->set_name(_name);
    pose->mutable_position()->set_x(_source.Pos().X());
    pose->mutable_position()->set_y(_source.Pos().Y());
    pose->mutable_position()->set_z(_source.Pos().Z());
    pose->mutable_orientation()->set_x(_source.Rot().X());
    pose->mutable_orientation()->set_y(_source.Rot().Y());
    pose->mutable_orientation()->set_z(_source.Rot().Z());
    pose->mutable_orientation()->set_w(_source.Rot().W());
  }

  ignition::transport::Node node;
  ignition::transport::Node::Publisher publisher;
  std::string robotName;
  std::string actorName;
  std::chrono::steady_clock::duration publishPeriod{};
  std::chrono::steady_clock::duration nextPublication{};
};
}

IGNITION_ADD_PLUGIN(
  malbut::gazebo::BenchmarkGroundTruthSystem,
  ignition::gazebo::System,
  ignition::gazebo::ISystemConfigure,
  ignition::gazebo::ISystemPostUpdate)
