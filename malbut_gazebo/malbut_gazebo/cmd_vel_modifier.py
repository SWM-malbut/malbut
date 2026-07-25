import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist


class CmdVelModifier(Node):
    def __init__(self):
        super().__init__('cmd_vel_modifier')

        # 订阅 `cmd_vel` 话题
        self.subscription = self.create_subscription(
            Twist,
            'cmd_vel',  # 输入话题名称
            self.cmd_vel_callback,
            10  # 队列大小
        )
        self.subscription  

        # 发布到 `cmd_vel_modified` 话题
        self.publisher = self.create_publisher(
            Twist,
            'cmd_vel_nav',  # 输出话题名称
            10  # 队列大小
        )

        # 增加的倍数（可以根据需要调整）
        self.linear_multiplier = 1
        self.angular_multiplier = 1

        self.get_logger().info("CmdVelModifier node has started.")

    def cmd_vel_callback(self, msg):
        # 修改接收到的速度
        modified_msg = Twist()
        modified_msg.linear.x = msg.linear.x * self.linear_multiplier
        modified_msg.linear.y = msg.linear.y * self.linear_multiplier
        modified_msg.linear.z = msg.linear.z * self.linear_multiplier
        modified_msg.angular.x = msg.angular.x * self.angular_multiplier
        modified_msg.angular.y = msg.angular.y * self.angular_multiplier
        modified_msg.angular.z = msg.angular.z * self.angular_multiplier

        # 发布修改后的消息
        self.publisher.publish(modified_msg)

        # 日志输出
        self.get_logger().info(
            f"Received cmd_vel: linear_x={msg.linear.x}, angular_z={msg.angular.z} | "
            f"Published modified cmd_vel: linear_x={modified_msg.linear.x}, angular_z={modified_msg.angular.z}"
        )


def main(args=None):
    rclpy.init(args=args)

    # 创建节点
    cmd_vel_modifier = CmdVelModifier()

    # 保持节点运行
    try:
        rclpy.spin(cmd_vel_modifier)
    except KeyboardInterrupt:
        cmd_vel_modifier.get_logger().info("Node stopped by user.")
    finally:
        # 销毁节点并关闭
        cmd_vel_modifier.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
