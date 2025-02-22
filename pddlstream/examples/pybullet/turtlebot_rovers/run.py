#!/usr/bin/env python

from __future__ import print_function

import argparse
import cProfile
import os
import pstats
import sys

sys.path.append(os.environ['PDDLSTREAM_PATH'])

from examples.pybullet.utils.pybullet_tools.pr2_primitives import Conf, control_commands, Attach, Detach
from examples.pybullet.utils.pybullet_tools.utils import connect, disconnect, \
    HideOutput, LockRenderer, wait_for_user
from pddlstream.algorithms.incremental import solve_incremental
from pddlstream.algorithms.focused import solve_focused
from pddlstream.language.generator import from_gen_fn, from_fn, from_test
from pddlstream.language.constants import And, print_solution, Exists
from pddlstream.utils import read, INF, get_file_path
from pddlstream.language.stream import StreamInfo

from examples.pybullet.pr2_belief.problems import BeliefState
from examples.pybullet.pr2_belief.primitives import Register, Scan

from examples.pybullet.utils.pybullet_tools.pr2_primitives import apply_commands
from examples.pybullet.utils.pybullet_tools.utils import draw_base_limits, WorldSaver, get_bodies
from examples.pybullet.namo.stream import get_custom_limits as get_base_custom_limits

from .problems import PROBLEMS, get_base_joints, KINECT_FRAME
from .streams import get_reachable_test, get_inv_vis_gen, get_inv_com_gen, \
    get_above_gen, get_motion_fn, get_cfree_ray_test, VIS_RANGE

CLASSES = [
    'blue', 'red', 'rock', 'soil',
]

BASE_LINK = 'base_link'

# https://github.com/erwincoumans/pybullet_robots/tree/master/data/turtlebot
# https://github.com/erwincoumans/pybullet_robots/tree/master/data/f10_racecar
# https://github.com/erwincoumans/bullet3/tree/master/data/racecar
# Logistics ICAPS domain

#######################################################

def pddlstream_from_problem(problem, collisions=True, teleport=False):
    # TODO: push and attach to movable objects

    domain_pddl = read(get_file_path(__file__, 'domain.pddl'))
    stream_pddl = read(get_file_path(__file__, 'stream.pddl'))
    constant_map = {}

    camera = 'RGBD'
    mode = 'color'  # highres | lowres | color | depth

    init = []
    goal_literals = [
        #('Calibrated', camera, problem.rovers[0])
        #('Analyzed', problem.rovers[0], problem.rocks[0])
        #('ReceivedAnalysis', problem.rocks[0])
        Exists(['?rock'], And(('Type', '?rock', 'stone'),
                              ('ReceivedAnalysis', '?rock'))),
        Exists(['?soil'], And(('Type', '?soil', 'soil'),
                              ('ReceivedAnalysis', '?soil'))),
    ]

    init += [('Type', b, ty)  for b, ty in problem.body_types]
    init += [('Lander', l) for l in problem.landers]
    init += [('Camera', camera), ('Supports', camera, mode), ('Mode', mode)]
    for rover in problem.rovers:
        base_joints = get_base_joints(rover)
        q0 = Conf(rover, base_joints)
        init += [('Rover', rover), ('OnBoard', camera, rover),
                 ('Conf', rover, q0), ('AtConf', rover, q0)]
        goal_literals += [('AtConf', rover, q0)]
    for store in problem.stores:
        init += [('Store', store)]
        init += [('Free', rover, store) for rover in problem.rovers]
        goal_literals += [('Free', rover, store) for rover in problem.rovers]
    for name in problem.objectives:
        init += [('Objective', name)]
        goal_literals += [('ReceivedImage', name, mode)]
    for name in problem.rocks:
        init += [('Rock', name)]
    goal_formula = And(*goal_literals)

    custom_limits = {}
    if problem.limits is not None:
        for rover in problem.rovers:
            custom_limits.update(get_base_custom_limits(rover, problem.limits))

    stream_map = {
        'test-cfree-ray-conf': from_test(get_cfree_ray_test(problem, collisions=collisions)),
        'test-reachable': from_test(get_reachable_test(problem, custom_limits=custom_limits, collisions=collisions, teleport=teleport)),
        'obj-inv-visible': from_gen_fn(get_inv_vis_gen(problem, custom_limits=custom_limits, collisions=collisions)),
        'com-inv-visible': from_gen_fn(get_inv_com_gen(problem, custom_limits=custom_limits, collisions=collisions)),
        'sample-above': from_gen_fn(get_above_gen(problem, custom_limits=custom_limits, collisions=collisions)),
        'sample-motion': from_fn(get_motion_fn(problem, custom_limits=custom_limits, collisions=collisions, teleport=teleport)),
    }
    #stream_map = 'debug'

    return domain_pddl, constant_map, stream_pddl, stream_map, init, goal_formula

#######################################################

def post_process(problem, plan, teleport=False):
    if plan is None:
        return None
    commands = []
    attachments = {}
    for i, (name, args) in enumerate(plan):
        if name == 'send_image':
            v, q, y, l, o, m = args
            new_commands = [y]
        elif name == 'send_analysis':
            v, q, y, l, r = args
            new_commands = [y]
        elif name == 'sample_rock':
            v, q, r, s = args
            attachments[v] = r
            new_commands = [Attach(v, arm=BASE_LINK, grasp=None, body=attachments[v])]
        elif name == 'drop_rock':
            # TODO: make a drop all rocks
            v, s = args
            new_commands = [Detach(v, arm=BASE_LINK, body=attachments[v])]
        elif name == 'calibrate':
            v, bq, y, o, c = args
            new_commands = [Register(v, o, camera_frame=KINECT_FRAME, max_depth=VIS_RANGE)]
        elif name == 'take_image':
            v, bq, y, o, c, m = args
            new_commands = [Scan(v, o, detect=False, camera_frame=KINECT_FRAME)]
        elif name == 'move':
            v, q1, t, q2 = args
            new_commands = [t]
        else:
            raise ValueError(name)
        print(i, name, args, new_commands)
        commands += new_commands
    return commands

#######################################################

def main(display=True, teleport=False):
    parser = argparse.ArgumentParser()
    parser.add_argument('-problem', default='rovers1', help='The name of the problem to solve')
    parser.add_argument('-algorithm', default='focused', help='Specifies the algorithm')
    parser.add_argument('-cfree', action='store_true', help='Disables collisions')
    parser.add_argument('-deterministic', action='store_true', help='Uses a deterministic sampler')
    parser.add_argument('-optimal', action='store_true', help='Runs in an anytime mode')
    parser.add_argument('-t', '--max_time', default=120, type=int, help='The max time')
    parser.add_argument('-unit', action='store_true', help='Uses unit costs')
    parser.add_argument('-simulate', action='store_true', help='Simulates the system')
    parser.add_argument('-viewer', action='store_true', help='enable the viewer while planning')
    args = parser.parse_args()
    print(args)

    problem_fn_from_name = {fn.__name__: fn for fn in PROBLEMS}
    if args.problem not in problem_fn_from_name:
        raise ValueError(args.problem)
    problem_fn = problem_fn_from_name[args.problem]
    connect(use_gui=args.viewer)
    with HideOutput():
        problem = problem_fn()
    saver = WorldSaver()
    draw_base_limits(problem.limits, color=(1, 0, 0))

    pddlstream_problem = pddlstream_from_problem(problem, collisions=not args.cfree, teleport=teleport)
    stream_info = {
        'test-cfree-ray-conf': StreamInfo(negate=True),
        'test-reachable': StreamInfo(p_success=1e-1),
        'obj-inv-visible': StreamInfo(),
        'com-inv-visible': StreamInfo(),
        'sample-above': StreamInfo(),
        'sample-motion': StreamInfo(overhead=10),
    }
    _, _, _, stream_map, init, goal = pddlstream_problem
    print('Init:', init)
    print('Goal:', goal)
    #print('Streams:', stream_map.keys())

    success_cost = 0 if args.optimal else INF
    planner = 'ff-wastar3'
    search_sample_ratio = 2
    max_planner_time = 10

    # TODO: need to accelerate samples here because of the failed test reachable

    pr = cProfile.Profile()
    pr.enable()
    with LockRenderer(False):
        if args.algorithm == 'focused':
            # TODO: option to only consider costs during local optimization
            solution = solve_focused(pddlstream_problem, stream_info=stream_info,
                                     planner=planner, max_planner_time=max_planner_time, debug=False,
                                     unit_costs=args.unit, success_cost=success_cost,
                                     max_time=args.max_time, verbose=True,
                                     unit_efforts=True, effort_weight=1,
                                     #bind=True, max_skeletons=None,
                                     search_sample_ratio=search_sample_ratio)
        elif args.algorithm == 'incremental':
            solution = solve_incremental(pddlstream_problem,
                                         planner=planner, max_planner_time=max_planner_time,
                                         unit_costs=args.unit, success_cost=success_cost,
                                         max_time=args.max_time, verbose=True)
        else:
            raise ValueError(args.algorithm)

    print_solution(solution)
    plan, cost, evaluations = solution
    pr.disable()
    pstats.Stats(pr).sort_stats('tottime').print_stats(25) # cumtime | tottime
    if plan is None:
        return
    if (not display) or (plan is None):
        disconnect()
        return

    # Maybe openrave didn't actually sample any joints...
    # http://openrave.org/docs/0.8.2/openravepy/examples.tutorial_iksolutions/
    with LockRenderer():
        commands = post_process(problem, plan, teleport=teleport)
        saver.restore()  # Assumes bodies are ordered the same way
    if not args.viewer:
        disconnect()
        connect(use_gui=True)
        with LockRenderer():
            with HideOutput():
                problem_fn() # TODO: way of doing this without reloading?
            saver.restore() # Assumes bodies are ordered the same way

    if args.simulate:
        control_commands(commands)
    else:
        time_step = None if teleport else 0.01
        apply_commands(BeliefState(problem), commands, time_step)
    wait_for_user()
    disconnect()

if __name__ == '__main__':
    main()
