#include <atomic>
#include <string>

#include <ignition/gazebo/EntityComponentManager.hh>
#include <ignition/gazebo/System.hh>
#include <ignition/gazebo/components/Actor.hh>
#include <ignition/gazebo/components/Name.hh>
#include <ignition/msgs/boolean.pb.h>
#include <ignition/msgs/empty.pb.h>
#include <ignition/plugin/Register.hh>
#include <ignition/transport/Node.hh>
#include <sdf/Element.hh>

namespace malbut::gazebo
{
class ActorControlSystem final:
  public ignition::gazebo::System,
  public ignition::gazebo::ISystemConfigure,
  public ignition::gazebo::ISystemPreUpdate
{
public:
  void Configure(
    const ignition::gazebo::Entity &,
    const std::shared_ptr<const sdf::Element> &_sdf,
    ignition::gazebo::EntityComponentManager &,
    ignition::gazebo::EventManager &) override
  {
    this->actorName = _sdf->Get<std::string>(
      "actor_name", "scenario_humanoid").first;
    this->servicePrefix = _sdf->Get<std::string>(
      "service_prefix", "/scenario_actor").first;
    this->node.Advertise(
      this->servicePrefix + "/exists",
      &ActorControlSystem::OnExists,
      this);
    this->node.Advertise(
      this->servicePrefix + "/remove",
      &ActorControlSystem::OnRemove,
      this);
  }

  void PreUpdate(
    const ignition::gazebo::UpdateInfo &,
    ignition::gazebo::EntityComponentManager &_ecm) override
  {
    bool found = false;
    _ecm.Each<
      ignition::gazebo::components::Actor,
      ignition::gazebo::components::Name>(
      [&](const ignition::gazebo::Entity &_entity,
          const ignition::gazebo::components::Actor *,
          const ignition::gazebo::components::Name *_name)
      {
        if (_name->Data() != this->actorName)
          return true;
        found = true;
        if (this->removeRequested.load())
          _ecm.RequestRemoveEntity(_entity, true);
        return true;
      });
    this->actorPresent.store(found);
    if (this->removeRequested.load() && !found)
      this->removeRequested.store(false);
  }

private:
  bool OnExists(
    const ignition::msgs::Empty &,
    ignition::msgs::Boolean &_response)
  {
    _response.set_data(this->actorPresent.load());
    return true;
  }

  bool OnRemove(
    const ignition::msgs::Empty &,
    ignition::msgs::Boolean &_response)
  {
    const bool present = this->actorPresent.load();
    if (present)
      this->removeRequested.store(true);
    _response.set_data(present);
    return true;
  }

  ignition::transport::Node node;
  std::string actorName;
  std::string servicePrefix;
  std::atomic_bool actorPresent{false};
  std::atomic_bool removeRequested{false};
};
}

IGNITION_ADD_PLUGIN(
  malbut::gazebo::ActorControlSystem,
  ignition::gazebo::System,
  ignition::gazebo::ISystemConfigure,
  ignition::gazebo::ISystemPreUpdate)
