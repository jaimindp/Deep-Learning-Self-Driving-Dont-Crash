from airsim_client import *
from rl_model import RlModel
import time
import numpy as np
import threading
import json
import os
import uuid
import glob
import datetime
import h5py
import sys
import requests
import PIL
import copy
import datetime
import airsim

# A class that represents the agent that will drive the vehicle, train the model, and send the gradient updates to the trainer.
class DistributedAgent():
    def __init__(self, parameters):
        required_parameters = ['data_dir', 'max_epoch_runtime_sec', 'replay_memory_size', 'batch_size', 'min_epsilon', 'per_iter_epsilon_reduction', 'experiment_name', 'train_conv_layers']
        for required_parameter in required_parameters:
            if required_parameter not in parameters:
                raise ValueError('Missing required parameter {0}'.format(required_parameter))

        parameters['role_type'] = 'agent'

        
        print('Starting time: {0}'.format(datetime.datetime.utcnow()), file=sys.stderr)
        self.__model_buffer = None
        self.__model = "sample_model.json"
        self.__airsim_started = False
        self.__data_dir = parameters['data_dir']
        self.__per_iter_epsilon_reduction = float(parameters['per_iter_epsilon_reduction'])
        self.__min_epsilon = float(parameters['min_epsilon'])
        self.__max_epoch_runtime_sec = float(parameters['max_epoch_runtime_sec'])
        self.__replay_memory_size = int(parameters['replay_memory_size'])
        self.__batch_size = int(parameters['batch_size'])
        self.__experiment_name = parameters['experiment_name']
        self.__train_conv_layers = bool((parameters['train_conv_layers'].lower().strip() == 'true'))
        self.__epsilon = 1
        self.__num_batches_run = 0
        self.__last_checkpoint_batch_count = 0
        
        if 'batch_update_frequency' in parameters:
            self.__batch_update_frequency = int(parameters['batch_update_frequency'])
        
        if 'weights_path' in parameters:
            self.__weights_path = parameters['weights_path']
        else:
            self.__weights_path = None
            
        if 'airsim_path' in parameters:
            self.__airsim_path = parameters['airsim_path']
        else:
            self.__airsim_path = None

        self.__local_run = 'local_run' in parameters

        self.__car_client = airsim.CarClient()
        self.__car_controls = airsim.CarControls()

        self.__minibatch_dir = os.path.join(self.__data_dir, 'minibatches')
        self.__output_model_dir = os.path.join(self.__data_dir, 'models')

        self.__make_dir_if_not_exist(self.__minibatch_dir)
        self.__make_dir_if_not_exist(self.__output_model_dir)
        self.__last_model_file = ''

        self.__possible_ip_addresses = []
        self.__trainer_ip_address = None

        self.__experiences = {}

        self.__init_road_points()
        self.__init_reward_points()

    # Starts the agent
    def start(self):
        self.__run_function()

    # The function that will be run during training.
    # It will initialize the connection to the trainer, start AirSim, and continuously run training iterations.
    def __run_function(self):
        print('Starting run function')
        
        # Once the trainer is online, it will write its IP to a file in (data_dir)\trainer_ip\trainer_ip.txt
        # Wait for that file to exist
        if not self.__local_run:
            while True:
                trainer_ip_dir = os.path.join(os.path.join(self.__data_dir, 'trainer_ip'), self.__experiment_name)
                print('Checking {0}...'.format(trainer_ip_dir))
                if os.path.isdir(trainer_ip_dir):
                    with open(os.path.join(trainer_ip_dir, 'trainer_ip.txt'), 'r') as f:
                        self.__possible_ip_addresses.append(f.read().replace('\n', ''))
                        break
                time.sleep(5)
        
            # We now have the IP address for the trainer. Attempt to ping the trainer.
            ping_idx = -1
            while True:
                ping_idx += 1
                try:
                    print('\tPinging {0}...'.format(self.__possible_ip_addresses[ping_idx % len(self.__possible_ip_addresses)]))
                    response = requests.get('http://{0}:80/ping'.format(self.__possible_ip_addresses[ping_idx % len(self.__possible_ip_addresses)])).json()
                    if response['message'] != 'pong':
                        raise ValueError('Received unexpected message: {0}'.format(response))
                    print('Success!')
                    self.__trainer_ip_address = self.__possible_ip_addresses[ping_idx % len(self.__possible_ip_addresses)]
                    break
                except Exception as e:
                    print('Could not get response. Message is {0}'.format(e))
                    if (ping_idx % len(self.__possible_ip_addresses) == 0):
                        print('Waiting 5 seconds and trying again...')
                        time.sleep(5)

            print('Getting model from the trainer')
            sys.stdout.flush()
            self.__model = RlModel(self.__weights_path, self.__train_conv_layers)
            self.__get_latest_model()
        
        else:
            print('Run is local. Skipping connection to trainer.')
            self.__model = RlModel(self.__weights_path, self.__train_conv_layers)
            

        self.__connect_to_airsim()

        while True:
            try:
                self.__run_airsim_epoch(True)
                percent_full = 100.0 * len(self.__experiences['actions'])/self.__replay_memory_size

                if (percent_full >= 100.0):
                    break
            except msgpackrpc.error.TimeoutError:
                self.__connect_to_airsim()

        
        if not self.__local_run:
            self.__get_latest_model()
        while True:
            try:
                if (self.__model is not None):
                    experiences, frame_count = self.__run_airsim_epoch(False)
                    # If we didn't immediately crash, train on the gathered experiences
                    if (frame_count > 0):
                        sampled_experiences = self.__sample_experiences(experiences, frame_count, True)
                        self.__num_batches_run += frame_count
                        # If we successfully sampled, train on the collected minibatches and send the gradients to the trainer node
                        if (len(sampled_experiences) > 0):
                            print('Publishing AirSim Epoch.')
                            self.__publish_batch_and_update_model(sampled_experiences, frame_count)

            except msgpackrpc.error.TimeoutError:
                print('Lost connection to AirSim. Attempting to reconnect.')
                self.__connect_to_airsim()

    # Connects to the AirSim Exe.
    def __connect_to_airsim(self):
        attempt_count = 0
        while True:
            try:
                print('Attempting to connect to AirSim (attempt {0})'.format(attempt_count))
                self.__car_client = airsim.CarClient()
                self.__car_client.confirmConnection()
                self.__car_client.enableApiControl(True)
                self.__car_controls = airsim.CarControls()
                return
            except:
                print('Failed to connect.')
                attempt_count += 1
                if (attempt_count % 10 == 0):
                    print('10 consecutive failures to connect. Attempting to start AirSim on my own.')
                    
                    if self.__local_run:
                        os.system('START "" powershell.exe {0}'.format(os.path.join(self.__airsim_path, 'AD_Cookbook_Start_AirSim.ps1 neighborhood -windowed')))
                    else:
                        os.system('START "" powershell.exe D:\\AD_Cookbook_AirSim\\Scripts\\DistributedRL\\restart_airsim_if_agent.ps1')
                time.sleep(10)

    # Appends a sample to a ring buffer.
    # If the appended example takes the size of the buffer over buffer_size, the example at the front will be removed.
    def __append_to_ring_buffer(self, item, buffer, buffer_size):
        if (len(buffer) >= buffer_size):
            buffer = buffer[1:]
        buffer.append(item)
        return buffer
    
    # Runs an interation of data generation from AirSim.
    # Data will be saved in the replay memory.
    def __run_airsim_epoch(self, always_random):
        print('Running AirSim epoch.')
        
        # Pick a random starting point on the roads
        starting_points, starting_direction = self.__get_next_starting_point()
        
        # Initialize the state buffer.
        # For now, save 4 images at 0.01 second intervals.
        state_buffer_len = 4
        state_buffer = []
        wait_delta_sec = 0.01

        self.__car_controls.steering = 0
        self.__car_controls.throttle = 0
        self.__car_controls.brake = 1
        self.__car_client.setCarControls(self.__car_controls)
        time.sleep(2)
        
        # While the car is rolling, start initializing the state buffer
        stop_run_time =datetime.datetime.now() + datetime.timedelta(seconds=2)
        while(datetime.datetime.now() < stop_run_time):
            time.sleep(wait_delta_sec)
            state_buffer = self.__append_to_ring_buffer(self.__get_image(), state_buffer, state_buffer_len)
        done = False
        actions = [] #records the state we go to
        pre_states = []
        post_states = []
        rewards = []
        predicted_rewards = []
        car_state = self.__car_client.getCarState()

        start_time = datetime.datetime.utcnow()
        end_time = start_time + datetime.timedelta(seconds=self.__max_epoch_runtime_sec)
        
        num_random = 0
        far_off = False
        
        # Main data collection loop
        while not done:
            collision_info = self.__car_client.simGetCollisionInfo()
            utc_now = datetime.datetime.utcnow()
            
            # Check for terminal conditions:
            # 1) Car has collided
            # 2) Car is stopped
            # 3) The run has been running for longer than max_epoch_runtime_sec. 
            # 4) The car has run off the road
            if (collision_info.has_collided or car_state.speed < 2 or far_off): # or utc_now > end_time or far_off):
                self.__car_client.reset()
                sys.stderr.flush()
            else:

                # The Agent should occasionally pick random action instead of best action
                do_greedy = np.random.random_sample()
                pre_state = copy.deepcopy(state_buffer)
                if (do_greedy < self.__epsilon or always_random):
                    num_random += 1
                    next_state = self.__model.get_random_state()
                    predicted_reward = 0
                    
                else:
                    next_state, predicted_reward = self.__model.predict_state(pre_state)
                    print('Model predicts {0}'.format(next_state))

                # Convert the selected state to a control signal
                next_control_signals = self.__model.state_to_control_signals(next_state, self.__car_client.getCarState())

                # Take the action
                self.__car_controls.steering = next_control_signals[0]
                self.__car_controls.throttle = next_control_signals[1]
                self.__car_controls.brake = next_control_signals[2]
                self.__car_client.setCarControls(self.__car_controls)
                
                # Wait for a short period of time to see outcome
                time.sleep(wait_delta_sec)

                # Observe outcome and compute reward from action
                state_buffer = self.__append_to_ring_buffer(self.__get_image(), state_buffer, state_buffer_len)
                car_state = self.__car_client.getCarState()
                collision_info = self.__car_client.simGetCollisionInfo()
                reward, far_off = self.__compute_reward(collision_info, car_state)
                
                # Add the experience to the set of examples from this iteration
                pre_states.append(pre_state)
                post_states.append(state_buffer)
                rewards.append(reward)
                predicted_rewards.append(predicted_reward)
                actions.append(next_state)

        # Only the last state is a terminal state.
        is_not_terminal = [1 for i in range(0, len(actions)-1, 1)]
        is_not_terminal.append(0)
        
        # Add all of the states from this iteration to the replay memory
        self.__add_to_replay_memory('pre_states', pre_states)
        self.__add_to_replay_memory('post_states', post_states)
        self.__add_to_replay_memory('actions', actions)
        self.__add_to_replay_memory('rewards', rewards)
        self.__add_to_replay_memory('predicted_rewards', predicted_rewards)
        self.__add_to_replay_memory('is_not_terminal', is_not_terminal)
        
        # If we are in the main loop, reduce the epsilon parameter so that the model will be called more often
        if not always_random:
            self.__epsilon -= self.__per_iter_epsilon_reduction
            self.__epsilon = max(self.__epsilon, self.__min_epsilon)
        
        return self.__experiences, len(actions)

    # Adds a set of examples to the replay memory
    def __add_to_replay_memory(self, field_name, data):
        if field_name not in self.__experiences:
            self.__experiences[field_name] = data
        else:
            self.__experiences[field_name] += data
            start_index = max(0, len(self.__experiences[field_name]) - self.__replay_memory_size)
            self.__experiences[field_name] = self.__experiences[field_name][start_index:]

    # Sample experiences from the replay memory
    def __sample_experiences(self, experiences, frame_count, sample_randomly):
        sampled_experiences = {}
        sampled_experiences['pre_states'] = []
        sampled_experiences['post_states'] = []
        sampled_experiences['actions'] = []
        sampled_experiences['rewards'] = []
        sampled_experiences['predicted_rewards'] = []
        sampled_experiences['is_not_terminal'] = []

        # Compute the surprise factor, which is the difference between the predicted an the actual Q value for each state.
        # We can use that to weight examples so that we are more likely to train on examples that the model got wrong.
        suprise_factor = np.abs(np.array(experiences['rewards'], dtype=np.dtype(float)) - np.array(experiences['predicted_rewards'], dtype=np.dtype(float)))
        suprise_factor_normalizer = np.sum(suprise_factor)
        suprise_factor /= float(suprise_factor_normalizer)

        # Generate one minibatch for each frame of the run
        for _ in range(0, frame_count, 1):
            if sample_randomly:
                idx_set = set(np.random.choice(list(range(0, suprise_factor.shape[0], 1)), size=(self.__batch_size), replace=False))
            else:
                idx_set = set(np.random.choice(list(range(0, suprise_factor.shape[0], 1)), size=(self.__batch_size), replace=False, p=suprise_factor))
        
            sampled_experiences['pre_states'] += [experiences['pre_states'][i] for i in idx_set]
            sampled_experiences['post_states'] += [experiences['post_states'][i] for i in idx_set]
            sampled_experiences['actions'] += [experiences['actions'][i] for i in idx_set]
            sampled_experiences['rewards'] += [experiences['rewards'][i] for i in idx_set]
            sampled_experiences['predicted_rewards'] += [experiences['predicted_rewards'][i] for i in idx_set]
            sampled_experiences['is_not_terminal'] += [experiences['is_not_terminal'][i] for i in idx_set]
            
        return sampled_experiences
        
     
    # Train the model on minibatches and post to the trainer node.
    def __publish_batch_and_update_model(self, batches, batches_count):
        # Train and get the gradients
        print('Publishing epoch data and getting latest model from parameter server...')
        gradients = self.__model.get_gradient_update_from_batches(batches)
        
        # Post the data to the trainer node
        if not self.__local_run:
            post_data = {}
            post_data['gradients'] = gradients
            post_data['batch_count'] = batches_count
            
            response = requests.post('http://{0}:80/gradient_update'.format(self.__trainer_ip_address), json=post_data)
            print('Response:')
            print(response)

            new_model_parameters = response.json()
            
            # Update the existing model with the new parameters
            self.__model.from_packet(new_model_parameters)
            
            #If the trainer sends us a epsilon, allow it to override our local value
            if ('epsilon' in new_model_parameters):
                new_epsilon = float(new_model_parameters['epsilon'])
                print('Overriding local epsilon with {0}, which was sent from trainer'.format(new_epsilon))
                self.__epsilon = new_epsilon
                
        else:
            if (self.__num_batches_run > self.__batch_update_frequency + self.__last_checkpoint_batch_count):
                self.__model.update_critic()
                
                checkpoint = {}
                checkpoint['model'] = self.__model.to_packet(get_target=True)
                checkpoint['batch_count'] = batches_count
                checkpoint_str = json.dumps(checkpoint)

                checkpoint_dir = os.path.join(os.path.join(self.__data_dir, 'checkpoint'), self.__experiment_name)
                
                if not os.path.isdir(checkpoint_dir):
                    try:
                        os.makedirs(checkpoint_dir)
                    except OSError as e:
                        if e.errno != errno.EEXIST:
                            raise
                            
                file_name = os.path.join(checkpoint_dir,'{0}.json'.format(self.__num_batches_run)) 
                with open(file_name, 'w') as f:
                    print('Checkpointing to {0}'.format(file_name))
                    f.write(checkpoint_str)
                
                self.__last_checkpoint_batch_count = self.__num_batches_run
                
    # Gets the latest model from the trainer node
    def __get_latest_model(self):
        print('Getting latest model from parameter server...')
        response = requests.get('http://{0}:80/latest'.format(self.__trainer_ip_address)).json()
        self.__model.from_packet(response)

    # Gets an image from AirSim
    def __get_image(self):
        image_response = self.__car_client.simGetImages([ImageRequest(0, AirSimImageType.Scene, False, False)])[0]
        image1d = np.fromstring(image_response.image_data_uint8, dtype=np.uint8)
        image_rgba = image1d.reshape(108,256,4)
        image_resized = image_rgba[49:108,0:255,0:3].astype(float)
        return image_resized

    # Computes the reward functinon based on the car position.
    def __compute_reward(self, collision_info, car_state):
        #Define some constant parameters for the reward function
        THRESH_DIST = 20                # The maximum distance from the center of the road to compute the reward function
        DISTANCE_DECAY_RATE = 1.2        # The rate at which the reward decays for the distance function
        CENTER_SPEED_MULTIPLIER = 2.0    # The ratio at which we prefer the distance reward to the speed reward

        # If the car has collided, the reward is always zero
        if (collision_info.has_collided):
            #return 0.0, True
            return -5.0, True
        
        # If the car is stopped, the reward is always zero
        speed = car_state.speed
        if (speed < 2):
            return 0.0, True
        
        #Get the car position
        position_key = bytes('position', encoding='utf8')
        x_val_key = bytes('x_val', encoding='utf8')
        y_val_key = bytes('y_val', encoding='utf8')

        #car_point = np.array([car_state.kinematics_true[position_key][x_val_key], car_state.kinematics_true[position_key][y_val_key], 0])
        car_point = np.array([car_state.kinematics_estimated.position.x_val, car_state.kinematics_estimated.position.y_val, 0])
        
        # Distance component is exponential distance to nearest line
        distance = 999
        
        #Compute the distance to the nearest center line
        for line in self.__reward_points:
            local_distance = 0
            length_squared = ((line[0][0]-line[1][0])**2) + ((line[0][1]-line[1][1])**2)
            if (length_squared != 0):
                t = max(0, min(1, np.dot(car_point-line[0], line[1]-line[0]) / length_squared))
                proj = line[0] + (t * (line[1]-line[0]))
                local_distance = np.linalg.norm(proj - car_point)
            distance = min(local_distance, distance)
            
        distance_reward = math.exp(-(distance * DISTANCE_DECAY_RATE))
        if(distance_reward<THRESH_DIST):
            distance_reward *= 10
        return distance_reward, distance > THRESH_DIST

    # Initializes the points used for determining the starting point of the vehicle
    def __init_road_points(self):
        self.__road_points = []
        car_start_coords = [12961.722656, 6660.329102, 0]
        with open(os.path.join(os.path.join(self.__data_dir, 'data'), 'road_lines.txt'), 'r') as f:
            for line in f:
                points = line.split('\t')
                first_point = np.array([float(p) for p in points[0].split(',')] + [0])
                second_point = np.array([float(p) for p in points[1].split(',')] + [0])
                self.__road_points.append(tuple((first_point, second_point)))

        # Points in road_points.txt are in unreal coordinates
        # But car start coordinates are not the same as unreal coordinates
        for point_pair in self.__road_points:
            for point in point_pair:
                point[0] -= car_start_coords[0]
                point[1] -= car_start_coords[1]
                point[0] /= 100
                point[1] /= 100
              
    # Initializes the points used for determining the optimal position of the vehicle during the reward function
    def __init_reward_points(self):
        self.__reward_points = []
        with open(os.path.join(os.path.join(self.__data_dir, 'data'), 'reward_points.txt'), 'r') as f:
            for line in f:
                point_values = line.split('\t')
                first_point = np.array([float(point_values[0]), float(point_values[1]), 0])
                second_point = np.array([float(point_values[2]), float(point_values[3]), 0])
                self.__reward_points.append(tuple((first_point, second_point)))

    # Randomly selects a starting point on the road
    # Used for initializing an iteration of data generation from AirSim
    def __get_next_starting_point(self):
        car_state = self.__car_client.getCarState()
        random_line_index = np.random.randint(0, high=len(self.__road_points))
        random_interp = (np.random.random_sample() * 0.4) + 0.3
        random_direction_interp = np.random.random_sample()
        random_line = self.__road_points[random_line_index]
        random_start_point = list(random_line[0])
        random_start_point[0] += (random_line[1][0] - random_line[0][0])*random_interp
        random_start_point[1] += (random_line[1][1] - random_line[0][1])*random_interp

        # Compute the direction that the vehicle will face
        # Vertical line
        if (np.isclose(random_line[0][1], random_line[1][1])):
            if (random_direction_interp > 0.5):
                random_direction = (0,0,0)
            else:
                random_direction = (0, 0, math.pi)
        # Horizontal line
        elif (np.isclose(random_line[0][0], random_line[1][0])):
            if (random_direction_interp > 0.5):
                random_direction = (0,0,math.pi/2)
            else:
                random_direction = (0,0,-1.0 * math.pi/2)

        # The z coordinate is always zero
        random_start_point[2] = -0
        return (random_start_point, random_direction)

    # A helper function to make a directory if it does not exist
    def __make_dir_if_not_exist(self, directory):
        if not (os.path.exists(directory)):
            try:
                os.makedirs(directory)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise

# Parse the command line parameters
parameters = {}
for arg in sys.argv:
    if '=' in arg:
        args = arg.split('=')
        print('0: {0}, 1: {1}'.format(args[0], args[1]))
        parameters[args[0].replace('--', '')] = args[1]
    if arg.replace('-', '') == 'local_run':
        parameters['local_run'] = True

#Make the debug statements easier to read
np.set_printoptions(threshold=sys.maxsize, suppress=True)

# Check additional parameters needed for local run
if 'local_run' in parameters:
    if 'airsim_path' not in parameters:
        print('ERROR: for a local run, airsim_path must be defined.')
        print('Please provide the path to airsim in a parameter like "airsim_path=<path_to_airsim>"')
        print('It should point to the folder containing AD_Cookbook_Start_AirSim.ps1')
        sys.exit()
    if 'batch_update_frequency' not in parameters:
        print('ERROR: for a local run, batch_update_frequency must be defined.')
        print('Please provide the path to airsim in a parameter like "batch_update_frequency=<int>"')
        sys.exit()

print('------------STARTING AGENT----------------')
print(parameters)

print('***')
print(os.environ)
print('***')

# Identify the node as an agent and start AirSim
if 'local_run' not in parameters:
    os.system('echo 1 >> D:\\agent.agent')
    os.system('START "" powershell.exe D:\\AD_Cookbook_AirSim\\Scripts\\DistributedRL\\restart_airsim_if_agent.ps1')
else:
    os.system('START "" powershell.exe {0}'.format(os.path.join(parameters['airsim_path'], 'AD_Cookbook_Start_AirSim.ps1 neighborhood -windowed')))
    
# Start the training
agent = DistributedAgent(parameters)
agent.start()
