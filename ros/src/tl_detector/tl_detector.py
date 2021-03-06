#!/usr/bin/env python
import rospy
from std_msgs.msg import Int32
from geometry_msgs.msg import PoseStamped, Pose
from styx_msgs.msg import TrafficLightArray, TrafficLight
from styx_msgs.msg import Lane
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from light_classification.tl_classifier import TLClassifier
import tf
import cv2
import yaml
import numpy as np

STATE_COUNT_THRESHOLD = 3

class TLDetector(object):
    def __init__(self):
        rospy.init_node('tl_detector')

        self.pose = None
        self.waypoints = []
        self.camera_image = None
        self.lights = []

        self.pose_sub = rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        self.base_wp_sub = rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)

        '''
        /vehicle/traffic_lights provides you with the location of the traffic light in 3D map space and 
        helps you acquire an accurate ground truth data source for the traffic light
        classifier by sending the current color state of all traffic lights in the
        simulator. When testing on the vehicle, the color state will not be available. You'll need to
        rely on the position of the light and the camera image to predict it.
        '''
        self.light_sub = rospy.Subscriber('/vehicle/traffic_lights', TrafficLightArray, self.traffic_cb)
        self.image_sub = rospy.Subscriber('/image_color', Image, self.image_cb)

        config_string = rospy.get_param("/traffic_light_config")
        self.config = yaml.load(config_string)

        self.upcoming_red_light_pub = rospy.Publisher('/traffic_waypoint', Int32, queue_size=1)

        self.bridge = CvBridge()
        self.light_classifier = TLClassifier()
        self.listener = tf.TransformListener()

        self.state = TrafficLight.UNKNOWN
        self.last_state = TrafficLight.UNKNOWN
        self.last_wp = -1
        self.state_count = 0

        self.light_waypoints = []
        self.light_indexed = False

        rospy.spin()

    def pose_cb(self, msg):
        self.pose = msg

    def index_lights(self):
        for light in self.lights:
            light_pos = light.pose.pose.position
            distances = []
            for i, wp in enumerate(self.waypoints):
                wp_pos = wp.pose.pose.position
                distance = (
                    (light_pos.x - wp_pos.x)**2
                    + (light_pos.y - wp_pos.y)**2
                    + (light_pos.z - wp_pos.z)**2
                )
                distances.append((i, distance))
            distances.sort(key=lambda d: d[1])
            waypoint_indices = map(lambda d: d[0], distances)
            self.light_waypoints.append(waypoint_indices)
        self.light_indexed = True

    def waypoints_cb(self, msg):
        self.waypoints = msg.waypoints
        self.base_wp_sub.unregister()

        if self.waypoints and self.lights and not self.light_indexed:
            self.index_lights()

    def traffic_cb(self, msg):
        self.lights = msg.lights

        if self.waypoints and self.lights and not self.light_indexed:
            self.index_lights()

    def image_cb(self, msg):
        """Identifies red lights in the incoming camera image and publishes the index
            of the waypoint closest to the red light to /traffic_waypoint

        Args:
            msg (Image): image from car-mounted camera

        """
        self.has_image = True
        self.camera_image = msg
        light_wp, state = self.process_traffic_lights()

        '''
        Publish upcoming red lights at camera frequency.
        Each predicted state has to occur `STATE_COUNT_THRESHOLD` number
        of times till we start using it. Otherwise the previous stable state is
        used.
        '''
        if self.state != state:
            self.state_count = 0
            self.state = state
        elif self.state_count >= STATE_COUNT_THRESHOLD:
            self.last_state = self.state
            light_wp = light_wp if state == TrafficLight.RED else -1
            self.last_wp = light_wp
            self.upcoming_red_light_pub.publish(Int32(light_wp))
        else:
            self.upcoming_red_light_pub.publish(Int32(self.last_wp))
        self.state_count += 1

    def get_closest_light(self, pose):
        """Identifies the closest path waypoint to the given position
            https://en.wikipedia.org/wiki/Closest_pair_of_points_problem
        Args:
            pose (Pose): position to match a waypoint to

        Returns:
            int: index of the closest waypoint in self.waypoints

        """
        #TODO implement
        ego_pos = np.array([
            self.pose.pose.position.x,
            self.pose.pose.position.y,
            self.pose.pose.position.z
        ])
        rot = tf.transformations.quaternion_matrix(np.array([
            self.pose.pose.orientation.x,
            self.pose.pose.orientation.y,
            self.pose.pose.orientation.z,
            self.pose.pose.orientation.w,
        ]))
        init_dir = np.array([1.0, 0., 0., 0.])
        ego_dir = np.matmul(init_dir, rot)[:3]
        min_dist = float('inf')
        light_index = None

        for i, light in enumerate(self.lights):
            light_pos = np.array([
                light.pose.pose.position.x, 
                light.pose.pose.position.y, 
                light.pose.pose.position.z])
            light_dir = light_pos - ego_pos
            if np.dot(ego_dir, light_dir) > 0:
                distance = np.linalg.norm(ego_pos - light_pos)
                if distance < min_dist:
                    light_index = i
                    min_dist = distance

        return light_index

    def project_to_image_plane(self, point_in_world):
        """Project point from 3D world coordinates to 2D camera image location

        Args:
            point_in_world (Point): 3D location of a point in the world

        Returns:
            x (int): x coordinate of target point in image
            y (int): y coordinate of target point in image

        """

        fx = self.config['camera_info']['focal_length_x']
        fy = self.config['camera_info']['focal_length_y']
        image_width = self.config['camera_info']['image_width']
        image_height = self.config['camera_info']['image_height']

        # get transform between pose of camera and world frame
        trans = None
        try:
            now = rospy.Time.now()
            self.listener.waitForTransform("/base_link",
                  "/world", now, rospy.Duration(1.0))
            (trans, rot) = self.listener.lookupTransform("/base_link",
                  "/world", now)

        except (tf.Exception, tf.LookupException, tf.ConnectivityException):
            rospy.logerr("Failed to find camera to map transform")

        #TODO Use tranform and rotation to calculate 2D position of light in image

        x = 0
        y = 0

        return (x, y)

    def get_light_state(self, light):
        """Determines the current color of the traffic light

        Args:
            light (TrafficLight): light to classify

        Returns:
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        if(not self.has_image):
            self.prev_light_loc = None
            return False

        cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")

        x, y = self.project_to_image_plane(light.pose.pose.position)

        #TODO use light location to zoom in on traffic light in image

        #Get classification
        # return self.light_classifier.get_classification(cv_image)
        return light.state

    def get_light_wp(self, light_index):
        way_points = self.light_waypoints[light_index]
        
        return way_points[0]

    def process_traffic_lights(self):
        """Finds closest visible traffic light, if one exists, and determines its
            location and color

        Returns:
            int: index of waypoint closes to the upcoming traffic light (-1 if none exists)
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        light_index = None

        if(self.pose):
            light_index = self.get_closest_light(self.pose.pose)

        if light_index is not None:
            light = self.lights[light_index]
            light_wp = self.get_light_wp(light_index)
            state = self.get_light_state(light)
            return light_wp, state

        return -1, TrafficLight.UNKNOWN

if __name__ == '__main__':
    try:
        TLDetector()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start traffic node.')
