from os import environ
from pprint import pprint
import pickle
import numpy as np
import torch 
import pandas as pd
import seaborn as sns
from torch import optim
import time
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
import math
import copy
import random
#for plotting
from IPython.display import clear_output
from matplotlib import pyplot as plt

import plotly.graph_objects as go
import sys
from torch.optim import AdamW
from torch.optim.lr_scheduler import *
import json
import re

train_device= torch.device(environ.get('train_device'))
store_device= torch.device(environ.get('store_device'))
dataset_file= environ.get('dataset_file')
test_dataset_file = environ.get('test_dataset_file')
benchmark_dataset_file=environ.get('benchmark_dataset_file')

#hyperparameter of the k-best model
k = 5
depth_max = 7
one_output = 22 # C{2,7} + 1= 22

#################################### Filtering functions for Data loading ####################################

#Filtering Loop Interchange schedules for the multiple computations functions
def filter_schedule_MC(schedule_str): # needs to return True if we want the passed schedule to be dropped 
    
    regex1 = "^I\(\{[C0-9,]+\},L[0-9]+,L[0-9]+\)$"
    regex2 = "F\(\{[C0-9,]+\},L[0-9]+\)I\(\{[C0-9,]+\},L[0-9]+,L[0-9]+\)$"
    if re.search(regex1, schedule_str) or re.search(regex2, schedule_str) : # drops all the schedules except the loop interchanges
        return True
    if schedule_str=="":
        return True
    return False


#Filtering Loop Interchange schedules for the single computations functions
def filter_schedule_SC(schedule_str): # needs to return True if we want the passed schedule to be dropped 
    
    regex = "I\(L[0-9]+,L[0-9]+\)$"
    if re.search(regex, schedule_str): # drops all the schedules except the loop interchanges
        return True
    if schedule_str=="":
        return True
    return False

#################################### Creating dataset entries functions ####################################

#define exception to discard the 'too large' programs for our presentation
class LargeAccessMatices(Exception):
    pass


#format the json file into arrays (X representation)
#program_dict added to retrieve the tree structure
def get_representation(program_json, schedule_json, program_dict):
    max_dims= depth_max
    max_accesses = 15
    program_representation = []
    indices_dict = dict()
    computations_dict = program_json['computations'] #dict_keys(['memory_size', 'iterators', 'computations'])
    ordered_comp_list = sorted(list(computations_dict.keys()), key = lambda x: computations_dict[x]['absolute_order'])
    
    for index, comp_name in enumerate(ordered_comp_list):
        comp_dict = computations_dict[comp_name]
        if len(comp_dict['accesses'])>max_accesses:
#             print('too much acc')
            raise LargeAccessMatices
        if len(comp_dict['accesses'])<1:
#             print('too little acc')
            raise LargeAccessMatices
        comp_representation = []
        #         Is this computation a reduction 
        comp_representation.append(+comp_dict['comp_is_reduction'])

        #fusion transformation -L
        fused_levels = []
        if 'fusions' in schedule_json and schedule_json['fusions']:
            for fusion in schedule_json['fusions']:#check if comp is involved in fusions 
                 # fusions format [compname1, compname2, loop depth]
                if comp_name in fusion:
                    fused_levels.append(fusion[2])
        
        iterators_repr = []
#         for iterator_name in comp_dict['iterators']: #-L
        for iter_i,iterator_name in enumerate(comp_dict['iterators']):
            
            iterator_dict = program_json['iterators'][iterator_name]
            iterators_repr.append(iterator_dict['upper_bound']) 
            iterators_repr.append(iterator_dict['lower_bound'])
            
            # Fusion representation -L
            if iter_i in fused_levels:
                iterators_repr.append(1) #fused true
            else:
                iterators_repr.append(0) #fused false
                            
        iterator_repr_size = int(len(iterators_repr)/len(comp_dict['iterators']))
        iterators_repr.extend([0]*iterator_repr_size*(max_dims-len(comp_dict['iterators']))) # adding iterators padding 

        comp_representation.extend(iterators_repr) #adding the iterators representation    

        #       write access represation 
        write_access_matrix = isl_to_write_matrix(comp_dict['write_access_relation'])
        write_access_matrix = np.array(write_access_matrix)
        write_access_matrix = np.c_[np.ones(write_access_matrix.shape[0]), write_access_matrix] # adding tags for marking the used rows
        write_access_matrix = np.r_[[np.ones(write_access_matrix.shape[1])], write_access_matrix] # adding tags for marking the used columns
        padded_write_matrix = np.zeros((max_dims + 1, max_dims + 2))
        padded_write_matrix[:write_access_matrix.shape[0],:write_access_matrix.shape[1]-1] = write_access_matrix[:,:-1] #adding padding to the access matrix
        padded_write_matrix[:write_access_matrix.shape[0],-1] = write_access_matrix[:,-1] #adding padding to the access matrix
        write_access_repr = [comp_dict['write_buffer_id']+1] + padded_write_matrix.flatten().tolist()
        comp_representation.extend(write_access_repr)
        
#         accesses representation
        accesses_repr=[]
        for access_dict in comp_dict['accesses']:
            access_matrix = access_dict['access_matrix']
            access_matrix = np.array(access_matrix)
            padded_access_matrix = np.zeros((max_dims, max_dims + 1))
            padded_access_matrix[:access_matrix.shape[0],:access_matrix.shape[1]-1] = access_matrix[:,:-1] #adding padding to the access matrix
            padded_access_matrix[:access_matrix.shape[0],-1] = access_matrix[:,-1] #adding padding to the access matrix
            access_repr = [access_dict['buffer_id']] + padded_access_matrix.flatten().tolist() # input_id + flattened access matrix 
            # is this access a reduction (the computation is accesing itself)
            access_repr.append(+access_dict['access_is_reduction'])
            accesses_repr.extend(access_repr)

        #access_repr_len = max_dims*(max_dims + 1)
        access_repr_len = max_dims*(max_dims + 1) + 1 +1 #+1 for input id, +1 for is_access_reduction
        accesses_repr.extend([0]*access_repr_len*(max_accesses-len(comp_dict['accesses']))) #adding accesses padding
    
        comp_representation.extend(accesses_repr) #adding access representation

#         operation histogram
        comp_representation.append(comp_dict['number_of_additions'])
        comp_representation.append(comp_dict['number_of_subtraction'])
        comp_representation.append(comp_dict['number_of_multiplication'])
        comp_representation.append(comp_dict['number_of_division'])
        
        #added in new reduced -L
        comp_representation.append(len(comp_dict['iterators'])) #the depth! 
   
        # adding log(x+1) of the representation
#         log_rep = list(np.log1p(comp_representation))
#         comp_representation.extend(log_rep)
        
        program_representation.append(comp_representation)
        indices_dict[comp_name] = index
    
    
    #added for fusion -L
#     # transforming the schedule_json in order to have loops as key instead of computations, this dict helps building the loop vectors
    loop_schedules_dict = dict()
    for loop_name in program_json['iterators']:
        loop_schedules_dict[loop_name]=dict()
        loop_schedules_dict[loop_name]['fused']=0
        
    for comp_index, comp_name in enumerate(ordered_comp_list):
        comp_schedule_dict = schedule_json[comp_name]

    #update the fusions in loops dict 
    if 'fusions' in schedule_json and schedule_json['fusions']:
        for fusion in schedule_json['fusions']:
            fused_loop1 = computations_dict[fusion[0]]['iterators'][fusion[2]]
            fused_loop2 = computations_dict[fusion[1]]['iterators'][fusion[2]]
            loop_schedules_dict[fused_loop1]['fused']=1
            loop_schedules_dict[fused_loop2]['fused']=1
    #end of added for fusion

    # collect the set of iterators that are used for computation (to eleminate those that are only used for inputs)
    real_loops = set()
    for comp_name in computations_dict:
        real_loops.update(computations_dict[comp_name]['iterators'])
        
    #building loop tensor
    loops_representation_list = []
    loops_indices_dict = dict()
    loop_index=0
    for loop_name in program_json['iterators']:
        if not (loop_name in real_loops): # this removes the iterators that are only used for decraling inputs
            continue
        loop_representation=[]
        loop_dict = program_json['iterators'][loop_name]
        # upper and lower bound
        loop_representation.append(loop_dict['upper_bound'])
        loop_representation.append(loop_dict['lower_bound'])
        
        #added for fusion
        loop_representation.append(loop_schedules_dict[loop_name]['fused'])
        
#      REMOVED IN NEW REDUCED REPRESENTATION        ----------------------------------------------
#         # adding log(x+1) of the loop representation
#         loop_log_rep = list(np.log1p(loop_representation)) #WHAT ?!! -L No need for log now! All numbers
#         loop_representation.extend(loop_log_rep)
#      REMOVED IN NEW REDUCED REPRESENTATION        ----------------------------------------------
        loops_representation_list.append(loop_representation)    
        loops_indices_dict[loop_name]=loop_index
        loop_index+=1
            
    def update_tree_atributes(node):     
        node['loop_index'] = torch.tensor(loops_indices_dict[node['loop_name'][:]]).to(train_device) ##whyyyyyyy ?!!!!!!
        if node['computations_list']!=[]:
            node['computations_indices'] = torch.tensor([indices_dict[comp_name] for comp_name in node['computations_list']]).to(train_device)
            node['has_comps'] = True
        else:
            node['has_comps'] = False
        for child_node in node['child_list']:
            update_tree_atributes(child_node)
        return node
    
    # getting the original tree structure 
    no_sched_json = program_dict['schedules_list'][0]
    assert 'fusions' not in no_sched_json or no_sched_json['fusions']==None
    orig_tree_structure = no_sched_json['tree_structure']
    tree_annotation = copy.deepcopy(orig_tree_structure) #to avoid altering the original tree from the json
    prog_tree = update_tree_atributes(tree_annotation) 
    
    
    #using resulting tree structure of the fused program
#     tree_annotation = copy.deepcopy(schedule_json['tree_structure']) #to avoid altering the original tree from the json
#     prog_tree = update_tree_atributes(tree_annotation) 
        
    
    loops_tensor = torch.unsqueeze(torch.FloatTensor(loops_representation_list),0)#.to(device)
    computations_tensor = torch.unsqueeze(torch.FloatTensor(program_representation),0)#.to(device)     

    return prog_tree, computations_tensor, loops_tensor


#return a string describing the tree structure (loops and computations placement) for the current program.
def get_tree_footprint(tree):
    footprint='<BL'+str(int(tree['loop_index']))
    if tree['has_comps']:
        footprint+='['
        for idx in tree['computations_indices']:
            footprint+='CI'+str(int(idx))
        footprint+=']'
    for child in tree['child_list']:
        footprint+= get_tree_footprint(child)
    footprint+='EL'+str(int(tree['loop_index']))+'>'
    return footprint
    
    
#dataset creation
class Dataset():
    def __init__(self, dataset_MC_filename, dataset_SC_filename, max_batch_size, filter_func=None, filter_func_MC=None,filter_func_SC=None, transform_func=None):
        super().__init__()
        
        self.X = []
        self.Y = []
        self.batched_program_names = []
        self.batched_schedule_names = []
        self.batched_exec_time = []
        self.nb_nan=0
        self.nb_long_access=0
        self.batches_dict=dict()
            
        
        if(dataset_MC_filename!=None):
        
            #loading multi computation programs

            self.dataset_MC_name=dataset_MC_filename

            if dataset_MC_filename.endswith('json'):
                with open(dataset_MC_filename, 'r') as f:
                    dataset_MC_str = f.read()
                self.programs_dict_MC = json.loads(dataset_MC_str)
            elif dataset_MC_filename.endswith('pkl'):
                with open(dataset_MC_filename, 'rb') as f:
                    self.programs_dict_MC = pickle.load(f)

            if (filter_func_MC==None):
                filter_func_MC = lambda x : True
            if (transform_func==None):
                transform_func = lambda x : x


            for function_name in tqdm(self.programs_dict_MC):
#                 print("MC : ", function_name)
                if (np.min(self.programs_dict_MC[function_name]['schedules_list'][0]['execution_times'])<0): #if less than x ms
                    continue

                program_json = self.programs_dict_MC[function_name]['program_annotation']
                program_exec_time = self.programs_dict_MC[function_name]['initial_execution_time']
                loops = shared_loop_nest(program_json)
                
                schedules_dict = {}  
                explored_schedules = {}

                #checking all schedules
                for schedule_index in range(len(self.programs_dict_MC[function_name]['schedules_list'])):
                    sched_str = self.programs_dict_MC[function_name]['schedules_list'][schedule_index]["sched_str"]
                    schedule_json = self.programs_dict_MC[function_name]['schedules_list'][schedule_index]

                    #leave only LI functions, that are legal: happen in the perfectly nested part of the loop nest.
                    if (not filter_func_MC(sched_str)) or (not legal_LI(sched_str, schedule_index, loops)):
                        continue

                    sched_exec_time = np.min(schedule_json['execution_times'])
                    self.programs_dict_MC[function_name]['schedules_list'][schedule_index]['speedup'] = max(program_exec_time / sched_exec_time,0.01) #speedup clipping
                    if ((np.isnan(self.programs_dict_MC[function_name]['schedules_list'][schedule_index]['speedup']))
                         or(self.programs_dict_MC[function_name]['schedules_list'][schedule_index]['speedup']==0)): 
                        self.nb_nan+=1
                        continue


                    #look for the parent schedule
                    scheduleP_str = re.sub("I\(\{[C0-9,]+\},L[0-9]+,L[0-9]+\)", '', sched_str)
#                         print(self.programs_dict_MC[function_name]['schedules_list'])
                    if scheduleP_str in explored_schedules.keys():  #parent schedule exist, check if the current schedule is better
                        schedulePID = explored_schedules[scheduleP_str]
            
                        # logging all LIs in this vector, to extract the best k LI later
                        ind_LI = index_interchage(sched_str)
                        if sched_exec_time < schedules_dict[schedulePID]['all'][ind_LI]:
                            schedules_dict[schedulePID]['all'][ind_LI] = sched_exec_time
                    
                        if sched_exec_time >= schedules_dict[schedulePID]['best']: # not the best
                            continue
                        else :
                            schedules_dict[schedulePID]['best'] = sched_exec_time
                            schedules_dict[schedulePID]['sched'] = schedule_index
#                             sched_dict = {'best':sched_exec_time, 'sched':schedule_index}
#                             schedules_dict[schedulePID] = sched_dict
                        

                    else: # parent schedule is new 
                        #tracking fusions
                        if scheduleP_str != "":
                            print(scheduleP_str, function_name)
                        
                        scheduleP, schedulePID = get_sched_by_string(scheduleP_str, self.programs_dict_MC[function_name]['schedules_list'])
#                         print(scheduleP, schedulePID )
                        explored_schedules[scheduleP_str]=schedulePID  # discoverd a new schedule

                        schedP_exec_time = np.min(scheduleP['execution_times'])

                        if sched_exec_time >= schedP_exec_time: # parent is best
                            schedP_dict = {'best':schedP_exec_time, 'sched':schedulePID}
                            schedules_dict[schedulePID] = schedP_dict
                        else : # schedule is best
                            sched_dict = {'best':sched_exec_time, 'sched':schedule_index}
                            schedules_dict[schedulePID] = sched_dict
                        
                        #create a vector, with 'one_output' size, to save execution times of all possible LIs, to be able to extract the k-best option later.
                        output = np.zeros(one_output,dtype=np.float64) + np.inf # because, a priori, all of them are very bad and does not execute well
                        output[0] = schedP_exec_time
                        ind_LI = index_interchage(sched_str)
                        output[ind_LI] = sched_exec_time
                        schedules_dict[schedulePID]['all'] = output

                #for each fused program or the original pgme
                for elem in schedules_dict.items():
                    sched_json = self.programs_dict_MC[function_name]['schedules_list'][elem[0]]
                    try:
                        tree, comps_tensor, loops_tensor = get_representation(program_json, sched_json, self.programs_dict_MC[function_name]) ######
                    except LargeAccessMatices:
                        self.nb_long_access +=1
                        continue   

                    # for each datapoint append its best LIs
                    tree_footprint=get_tree_footprint(tree) 
                    self.batches_dict[tree_footprint] = self.batches_dict.get(tree_footprint,{'tree':tree,'comps_tensor_list':[],'loops_tensor_list':[],'program_names_list':[],'sched_names_list':[],'speedups_list':[],'exec_time_list':[], 'output':[]}) 
                    self.batches_dict[tree_footprint]['comps_tensor_list'].append(comps_tensor)
                    self.batches_dict[tree_footprint]['loops_tensor_list'].append(loops_tensor)
#                     print(comps_tensor.shape, loops_tensor.shape)
                    self.batches_dict[tree_footprint]['sched_names_list'].append(elem[0])
                    self.batches_dict[tree_footprint]['program_names_list'].append(function_name)

                    speedup = max(program_exec_time / elem[1]['best'] , 0.01) #speedup clipping
                    self.batches_dict[tree_footprint]['speedups_list'].append(speedup)  
                    self.batches_dict[tree_footprint]['exec_time_list'].append(elem[1]['best'])

                    best_schedule_str = self.programs_dict_MC[function_name]['schedules_list'][elem[1]['sched']]['sched_str'] #does it contains only LI ?
                    
                    #produce the k-best concatinate output.
                    output= elem[1]['all']
                    output = output[0] / output 
                    order = np.flip(np.argsort(output, -1)) #highest to smallest
                    y = np.zeros(one_output * k,dtype=np.float64)
                    for i in range(k):
                        if output[order[i]] == 0:
                            y[one_output*i] = 1 # No LI
                            continue
                        y[one_output*i+order[i]] = 1 #define the output, using the best order we have in order % speedups. Because,it returns the indexes of the best options, and thus, the ones that should be put in 1.
                    self.batches_dict[tree_footprint]['output'].append(torch.tensor(y)) 

        #loading single computation programs
        if(dataset_SC_filename!=None):
            self.dataset_SC_name=dataset_SC_filename

            if dataset_SC_filename.endswith('json'):
                with open(dataset_SC_filename, 'r') as f:
                    dataset_SC_str = f.read()
                self.programs_dict_SC = json.loads(dataset_SC_str)
            elif dataset_SC_filename.endswith('pkl'):
                with open(dataset_SC_filename, 'rb') as f:
                    self.programs_dict_SC = pickle.load(f)

            if (filter_func_SC==None):
                filter_func_SC = lambda x : True
            if (transform_func==None):
                transform_func = lambda x : x


            for function_name in tqdm(self.programs_dict_SC):
                if (np.min(self.programs_dict_SC[function_name]['schedules_list'][0]['execution_times'])<0): #if less than x ms
                    continue
                program_json = self.programs_dict_SC[function_name]['program_annotation']
                program_exec_time = self.programs_dict_SC[function_name]['initial_execution_time']
                Parent_sched_json = self.programs_dict_SC[function_name]['schedules_list'][0]   # get the parent schedule


                try:
                    tree, comps_tensor, loops_tensor = get_representation(program_json, Parent_sched_json, self.programs_dict_SC[function_name]) ######
                except LargeAccessMatices:
                    self.nb_long_access +=1
                    continue        

                #######
                min_sch_time = np.inf   
                output = np.zeros(one_output,dtype=np.float64) + np.inf # because, a priori, all of them are very bad and does not execute well
                
                for schedule_index in range(len(self.programs_dict_SC[function_name]['schedules_list'])):
                    sched_str = sched_json_to_sched_str( self.programs_dict_SC[function_name]['schedules_list'][schedule_index] )
                    if (not filter_func_SC(sched_str)):
                        continue  

                    sched_exec_time = np.min(self.programs_dict_SC[function_name]['schedules_list'][schedule_index]['execution_times'])
                    self.programs_dict_SC[function_name]['schedules_list'][schedule_index]['speedup'] = max(program_exec_time / sched_exec_time,0.01) #speedup clipping
                    if ((np.isnan(self.programs_dict_SC[function_name]['schedules_list'][schedule_index]['speedup']))
                         or(self.programs_dict_SC[function_name]['schedules_list'][schedule_index]['speedup']==0)): #Nan value means the schedule didn't run, zero values means exec time<1 micro-second, skip them
                        self.nb_nan+=1
                        continue

                    #if another schedule used the same interchange before
                    if output[index_interchage(sched_str)] > sched_exec_time:
                        output[index_interchage(sched_str)] = sched_exec_time #position all the execution times
                
                    if sched_exec_time >= min_sch_time:
                            continue   # we only keep the best loop interchange for a given program
                    min_sch_time = sched_exec_time
                    best_schedule_str = sched_json_to_sched_str( self.programs_dict_SC[function_name]['schedules_list'][schedule_index] )
               
                # for each function append its best LI

                tree_footprint=get_tree_footprint(tree) 
                self.batches_dict[tree_footprint] = self.batches_dict.get(tree_footprint,{'tree':tree,'comps_tensor_list':[],'loops_tensor_list':[],'program_names_list':[],'sched_names_list':[],'speedups_list':[],'exec_time_list':[], 'output':[]}) 
                self.batches_dict[tree_footprint]['comps_tensor_list'].append(comps_tensor)
                self.batches_dict[tree_footprint]['loops_tensor_list'].append(loops_tensor)
#                 print(comps_tensor.shape, loops_tensor.shape)
                #self.batches_dict[tree_footprint]['sched_names_list'].append(schedule_name)  ommit it bcs the schedule is always the 0th schedule ==> not significant
                self.batches_dict[tree_footprint]['program_names_list'].append(function_name)
                self.batches_dict[tree_footprint]['speedups_list'].append(self.programs_dict_SC[function_name]['schedules_list'][0]['speedup'])  ######
                self.batches_dict[tree_footprint]['exec_time_list'].append(program_exec_time)   ######
                
                #Y
                output = output[0] / output 
                order = np.flip(np.argsort(output, -1)) #highest to smallest
                y = np.zeros(one_output * k,dtype=np.float64)
                for i in range(k):
                    if output[order[i]] == 0:
                        y[one_output*i] = 1 # No LI
                        continue
                    y[one_output*i+order[i]] = 1 #define the output, using the best order we have in order % speedups. Because,it returns the indexes of the best options, and thus, the ones that should be put in 1.
                self.batches_dict[tree_footprint]['output'].append(torch.tensor(y))

# Batching the data. 
        storing_device = store_device
        for tree_footprint in self.batches_dict:
            for chunk in range(0,len(self.batches_dict[tree_footprint]['program_names_list']),max_batch_size):  #####
                if storing_device.type=='cuda': # Check GPU memory in order to avoid Out of memory error
                    if ((torch.cuda.memory_allocated(storing_device.index)/torch.cuda.get_device_properties(storing_device.index).total_memory)>0.80):
                        print('GPU memory on '+str(storing_device)+' nearly full, switching to CPU memory')
                        storing_device = torch.device('cpu')
                #self.batched_schedule_names.append(self.batches_dict[tree_footprint]['sched_names_list'][chunk:chunk+max_batch_size])   #####
                self.batched_program_names.append(self.batches_dict[tree_footprint]['program_names_list'][chunk:chunk+max_batch_size])
                self.batched_exec_time.append(self.batches_dict[tree_footprint]['exec_time_list'][chunk:chunk+max_batch_size])
                T = self.batches_dict[tree_footprint]['tree'] # tree of the program
                R = torch.cat(self.batches_dict[tree_footprint]['comps_tensor_list'][chunk:chunk+max_batch_size], 0).to(storing_device) #computation tensor
                S = torch.cat(self.batches_dict[tree_footprint]['loops_tensor_list'][chunk:chunk+max_batch_size], 0).to(storing_device) #loops tensor
                self.X.append( ( T, R , S ) )
                    
                temp_tens = torch.cat(self.batches_dict[tree_footprint]['output'][chunk:chunk+max_batch_size])
                dim_temp_tens = int(temp_tens.shape[0] / (one_output * k))
                self.Y.append(torch.reshape(temp_tens, (dim_temp_tens,one_output *k)).to(storing_device))
                
                #sanity check
                if len(self.X) != len(self.Y):
                    print(len(self.X[-1]), len(self.Y[-1]))   # to look here 
                    print(type(self.X),len(self.X), type(self.Y),len(self.Y),type(self.X[0][1]),len(self.X[0][1]))
                    print("stop")
                                                
        print(f'Number of batches {len(self.Y)}')
        if self.nb_long_access>0:
            print('Number of batches dropped due to too much memory accesses:' +str(self.nb_long_access))
            
            
    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return [self[i] for i in range(start, stop, step)]
        elif isinstance(index, int):
            return self.X[index], self.Y[index] 

    def __len__(self):
        return len(self.Y)

    
#loading data of single computation structure (precedently used) and the multiple computation one.
def load_merge_data(train_val_MC,train_val_SC,split_ratio=None, max_batch_size=2048, filter_func_MC=None, filter_func_SC=None):
    full_dataset = Dataset(train_val_MC, train_val_SC, max_batch_size,filter_func_MC=filter_func_MC, filter_func_SC=filter_func_SC)
    if split_ratio == None:
        split_ratio=0.2
    if split_ratio > 1 : # not a ratio a number of batches
        validation_size = split_ratio
    else:
        validation_size = int(split_ratio * len(full_dataset))
    indices = list(range(len(full_dataset)))
    random.Random(42).shuffle(indices)
    val_batches_indices, train_batches_indices = indices[:validation_size],\
                                               indices[validation_size:]
    val_batches_list=[]
    train_batches_list=[]
    for i in val_batches_indices:
        val_batches_list.append(full_dataset[i])
    for i in train_batches_indices:
        train_batches_list.append(full_dataset[i])
    print("Data loaded")
    print("Sizes: "+str((len(val_batches_list),len(train_batches_list)))+" batches")
    return full_dataset, val_batches_list, val_batches_indices, train_batches_list, train_batches_indices


#################################### Training functions ####################################

def train_model(model, criterion, optimizer, max_lr, dataloader, num_epochs=100, log_every=5, logFile='log.txt'):
    since = time.time()    
    losses = []
    train_loss = 0
    best_loss = math.inf
    best_model = None
    soft = nn.Softmax(-1)
    
    dataloader_size = {'train':0,'val':0}
    for _,label in dataloader['train']: 
        dataloader_size['train']+=label.shape[0] ####
    for _,label in dataloader['val']:
        dataloader_size['val']+=label.shape[0] ####

    model = model.to(train_device)
    scheduler = OneCycleLR(optimizer, max_lr=max_lr, steps_per_epoch=len(dataloader['train']), epochs=num_epochs)
    for epoch in range(num_epochs):
        epoch_start=time.time()
        # Each epoch has a training and validation phase
        for phase in ['train', 'val']:
            if phase == 'train':                
                model.train()  
            else:
                model.eval()
            running_loss = 0.0         
            # Iterate over data. 
            for inputs, labels in dataloader[phase]:
                original_device = labels.device  ####
                inputs=(inputs[0], inputs[1].to(train_device), inputs[2].to(train_device))
                labels=labels.to(train_device)  ####
                # zero the parameter gradients
                optimizer.zero_grad()
                # forward
                # track history if only in train
                with torch.set_grad_enabled(phase == 'train'):
    
                    outputs = model(inputs)  
                    assert outputs.shape == labels.shape 
                    loss = 0
                    #apply cross entropy on each prediction in the sub-vectors.
                    for i in range(k):
                        loss += 100*criterion(outputs[...,one_output*i:one_output*(i+1)], torch.argmax(labels[...,one_output*i:one_output*(i+1)],-1))                     
                    # backward + optimize only if in training phase
                    if phase == 'train':
                        loss.backward()
                        optimizer.step()
                # statistics
                
                running_loss += loss.item()*labels.shape[0]
                #running_loss += loss.item()
                inputs=(inputs[0], inputs[1].to(original_device), inputs[2].to(original_device))
                labels=labels.to(original_device)
                epoch_end=time.time()                
                #running_corrects += torch.sum((outputs.data - labels.data) < e)/inputs.shape[0]
            epoch_loss = running_loss / dataloader_size[phase]           
            if phase == 'val':
                losses.append((train_loss, epoch_loss))
                if (epoch_loss<=best_loss):
                    best_loss = epoch_loss
                    best_model= copy.deepcopy(model)
                print('Epoch {}/{}:  train Loss: {:.4f}   val Loss: {:.4f}   time: {:.2f}s   best: {:.4f}'
                      .format(epoch + 1, num_epochs, train_loss, epoch_loss, epoch_end - epoch_start, best_loss))   
                if(len(losses)>2 and (epoch%100 == 0 )) :
                    loss_plot(losses)
                if epoch == (num_epochs - 1):
                    loss_plot(losses, end = True)                    
                if (epoch%log_every==0):
                    with open(logFile, "a+") as f:
                        f.write('Epoch {}/{}:  train Loss: {:.4f}   val Loss: {:.4f}   time: {:.2f}s   best: {:.4f} \n'
                      .format(epoch + 1, num_epochs, train_loss, epoch_loss, epoch_end - epoch_start, best_loss))
            else:
                train_loss = epoch_loss
                scheduler.step()
    time_elapsed = time.time() - since
    print('Training complete in {:.0f}m {:.0f}s   best validation loss: {:.4f}'
          .format(time_elapsed // 60, time_elapsed % 60, best_loss)) 
    with open(logFile, "a+") as f:
        f.write('-----> Training complete in {:.0f}m {:.0f}s   best validation loss: {:.4f}\n '
          .format(time_elapsed // 60, time_elapsed % 60, best_loss))
        
    return losses, best_model

#print the losses graph
def loss_plot(losses, figsize=(8,6), title='', end = False):   
    val_loss, train_loss = map(list, zip(*losses))  
    plt.figure(figsize=figsize)
    plt.plot(val_loss, label='Training Loss')
    plt.plot(train_loss, label='Validation Loss')
    plt.title(title)
    plt.grid(True)
    plt.xlabel('epoch')
    plt.legend(loc='center left') # the plot evolves to the right
    if end == True:
        plt.savefig("output.png")
    plt.show();
    

#################################### Model definition functions ####################################

class Model_Recursive_LSTM_v2(nn.Module):
    def __init__(self, input_size, comp_embed_layer_sizes=[600, 350, 200, 180], drops=[0.225, 0.225, 0.225, 0.225], output_size=1):
        super().__init__()
        embedding_size = comp_embed_layer_sizes[-1]
        regression_layer_sizes = [embedding_size] + comp_embed_layer_sizes[-2:]
#         concat_layer_sizes = [embedding_size*2+24] + comp_embed_layer_sizes[-2:] # before reduced -L
        concat_layer_sizes = [embedding_size*2+3] + comp_embed_layer_sizes[-2:]
        comp_embed_layer_sizes = [input_size] + comp_embed_layer_sizes
        self.comp_embedding_layers = nn.ModuleList()
        self.comp_embedding_dropouts= nn.ModuleList()
        self.regression_layers = nn.ModuleList()
        self.regression_dropouts= nn.ModuleList()
        self.concat_layers = nn.ModuleList()
        self.concat_dropouts= nn.ModuleList()
        for i in range(len(comp_embed_layer_sizes)-1):
            self.comp_embedding_layers.append(nn.Linear(comp_embed_layer_sizes[i], comp_embed_layer_sizes[i+1], bias=True))
            nn.init.xavier_uniform_(self.comp_embedding_layers[i].weight)
            self.comp_embedding_dropouts.append(nn.Dropout(drops[i]))
        for i in range(len(regression_layer_sizes)-1):
            self.regression_layers.append(nn.Linear(regression_layer_sizes[i], regression_layer_sizes[i+1], bias=True))
            nn.init.xavier_uniform_(self.regression_layers[i].weight)
            self.regression_dropouts.append(nn.Dropout(drops[i]))
        for i in range(len(concat_layer_sizes)-1):
            self.concat_layers.append(nn.Linear(concat_layer_sizes[i], concat_layer_sizes[i+1], bias=True))
#             nn.init.xavier_uniform_(self.concat_layers[i].weight)
            nn.init.zeros_(self.concat_layers[i].weight)
            self.concat_dropouts.append(nn.Dropout(drops[i]))
        self.predict = nn.Linear(regression_layer_sizes[-1], output_size, bias=True)
        nn.init.xavier_uniform_(self.predict.weight)
        self.ELU=nn.ELU()
        self.no_comps_tensor = nn.Parameter(nn.init.xavier_uniform_(torch.zeros(1, embedding_size)))
#         self.ELU = nn.Tanh()
        self.no_nodes_tensor = nn.Parameter(nn.init.xavier_uniform_(torch.zeros(1, embedding_size)))
        self.comps_lstm = nn.LSTM(comp_embed_layer_sizes[-1], embedding_size, batch_first=True)
        self.nodes_lstm = nn.LSTM(comp_embed_layer_sizes[-1], embedding_size, batch_first=True)
        
    def get_hidden_state(self, node, comps_embeddings, loops_tensor):
        nodes_list = []
        for n in node['child_list']:
            nodes_list.append(self.get_hidden_state(n, comps_embeddings,loops_tensor))
        if (nodes_list != []):
            nodes_tensor = torch.cat(nodes_list, 1) 
            lstm_out, (nodes_h_n, nodes_c_n) = self.nodes_lstm(nodes_tensor)
            nodes_h_n = nodes_h_n.permute(1,0,2)
        else:       
            nodes_h_n = torch.unsqueeze(self.no_nodes_tensor, 0).expand(comps_embeddings.shape[0], -1, -1)
        if (node['has_comps']):
            selected_comps_tensor = torch.index_select(comps_embeddings, 1, node['computations_indices'])
            lstm_out, (comps_h_n, comps_c_n) = self.comps_lstm(selected_comps_tensor) 
            comps_h_n = comps_h_n.permute(1,0,2)
        else:
            comps_h_n = torch.unsqueeze(self.no_comps_tensor, 0).expand(comps_embeddings.shape[0], -1, -1)
        selected_loop_tensor = torch.index_select(loops_tensor,1,node['loop_index'])
        x = torch.cat((nodes_h_n, comps_h_n, selected_loop_tensor),2)
        for i in range(len(self.concat_layers)):
            x = self.concat_layers[i](x)
            x = self.concat_dropouts[i](self.ELU(x))
        return x  

    def forward(self, tree_tensors):
        tree, comps_tensor, loops_tensor = tree_tensors
        #computation embbedding layer
        x = comps_tensor
        for i in range(len(self.comp_embedding_layers)):
            x = self.comp_embedding_layers[i](x)
            x = self.comp_embedding_dropouts[i](self.ELU(x))  
        comps_embeddings = x
        #recursive loop embbeding layer
        prog_embedding = self.get_hidden_state(tree, comps_embeddings, loops_tensor)
        #regression layer
        x = prog_embedding
        for i in range(len(self.regression_layers)):
            x = self.regression_layers[i](x)
            x = self.regression_dropouts[i](self.ELU(x))
        out = self.predict(x)
#         print(out[:,0,:].shape)
        return out[:,0,:]  #(out[:,0])#self.ELU(out[:,0,0])       nb_elem/batch  nb_comp 15
    

#################################### Results retrieval functions ####################################
    
def get_results_df(dataset, batches_list, indices, model, log=False):   
    df = pd.DataFrame()
    model.eval()
    torch.set_grad_enabled(False)
    all_outputs=[]
    all_labels=[]
    prog_names=[]
    #sched_names=[]
    exec_times=[]
    soft = nn.Softmax(-1)

    for kk, (inputs, labels) in tqdm(list(enumerate(batches_list))):
        original_device = labels.device
        inputs=(inputs[0], inputs[1].to(train_device), inputs[2].to(train_device))
        labels=labels.to(train_device)
        outputs = model(inputs)
        assert outputs.shape == labels.shape
        
        outputs_soft = soft(outputs[...,0:one_output]) # special case
        for i in range(1,k):
            outputs_soft = torch.cat((outputs_soft,soft(outputs[...,one_output*i:one_output*(i+1)])),-1)
        print(outputs_soft.shape, labels.shape)
        assert outputs_soft.shape == labels.shape

        #verified: outputs_softmax has the same shape
        
        all_outputs.append(outputs_soft) ##fixed
        all_labels.append(labels)

        for j, prog_name in enumerate(dataset.batched_program_names[indices[kk]]):   #####
            #sched_names.append(sched_name)
            prog_names.append(dataset.batched_program_names[indices[kk]][j])
            exec_times.append(dataset.batched_exec_time[indices[kk]][j])
        inputs=(inputs[0], inputs[1].to(original_device), inputs[2].to(original_device))
        labels=labels.to(original_device)
        
    preds = torch.cat(all_outputs).cpu().detach().numpy() #because it is modified above to be a tensor
    targets = torch.cat(all_labels).cpu().detach().numpy()
                                      
    assert preds.shape == targets.shape 
    df['name'] = prog_names
    df['exec_time'] = exec_times
    df['prediction'] = list(format_output(preds)[:,:]) #MISTAKES 
    df['target'] = list(targets[:,:])
    return df

#################################### Helper functions for data representation ####################################

def isl_to_write_matrix(isl_map): # for now this function only support reductions
    comp_iterators_str = re.findall(r'\[(.*)\]\s*->', isl_map)[0]
    buffer_iterators_str = re.findall(r'->\s*\w*\[(.*)\]', isl_map)[0]
    buffer_iterators_str=re.sub(r"\w+'\s=","",buffer_iterators_str)
    comp_iter_names = re.findall(r'(?:\s*(\w+))+', comp_iterators_str)
    buf_iter_names = re.findall(r'(?:\s*(\w+))+', buffer_iterators_str)
    matrix = np.zeros([len(buf_iter_names),len(comp_iter_names)+1])
    for i,buf_iter in enumerate(buf_iter_names):
        for j,comp_iter in enumerate(comp_iter_names):
            if buf_iter==comp_iter:
                matrix[i,j]=1
                break
    return matrix

def isl_to_write_dims(isl_map): # return the buffer iterator that defines the write buffer
    buffer_iterators_str = re.findall(r'->\s*\w*\[(.*)\]', isl_map)[0]
    buffer_iterators_str = re.sub(r"\w+'\s=","",buffer_iterators_str)
    buf_iter_names = re.findall(r'(?:\s*(\w+))+', buffer_iterators_str)
    return buf_iter_names
                              

#################################### Helper functions for LI output ####################################

# return the position of a chosen LI  in the output vector
def pos(a,depth):  # calculate the position of the interchange in the output vector
    if(a==0):
        return 0
    elif (a==1):
        return depth-1
    else:
        return pos(a-1,depth)+depth-a

#encode the output vector to get '1' in the right LI, '0' elsewhere
def encode_interchage(str_sched):
    output = np.zeros(one_output,dtype=int) 
    str_interchange = re.findall('I\(.*\)', str_sched)  # get the LI string
    if str_interchange==[]:
        output[0] = 1
    else:
        m = re.findall(r'\d+', str_interchange[0])  # get the number of the loops
        a = min(int(m[-2]),int(m[-1]))
        b = max(int(m[-2]),int(m[-1]))
        i2 = b-a
        i1 = pos(a,depth_max)
        output[i1+i2]=1      
    return output

#same as precedent, but return the index
def index_interchage(str_sched): #encode the output vector to get '1' in the right LI, '0' elsewhere
    output = np.zeros(one_output,dtype=int)
    str_interchange = re.findall('I\(.*\)', str_sched)  # get the LI string
    if str_interchange==[]:
        return(0)
    else:
        m = re.findall(r'\d+', str_interchange[0])  # get the number of the loops
        a = min(int(m[-2]),int(m[-1]))
        b = max(int(m[-2]),int(m[-1]))
        i2 = b-a
        i1 = pos(a,depth_max)
        return(i1+i2)      

    
def get_sched_by_string(sched_str,schedules):
    for schedID in range(len(schedules)):
        sched = schedules[schedID]
        schedExp_str = sched['sched_str']
        if schedExp_str == sched_str: # found the right schedule
            return sched, schedID
        
def get_results_times_function(vector,schedules):
    
    sched_str = "I(L"
    np_vect= np.array(vector)
    indices = np.where(np_vect==1)[0]
    if len(indices) != 0: # check if a LI is applied
        sched_str = sched_str + str(indices[0]) + ",L" + str(indices[1]) + ")"
    else :
        sched_str = ""
        
    sched = get_sched_by_string(sched_str,schedules)
    if sched != None :
        sched_exec_time = np.min(sched['execution_times']) 
        return sched_exec_time
    else :
        return -1
    

def format_output(arr):  
    pred = np.zeros(arr.shape)
    for b in range(k):
        indices = np.argmax(arr[...,one_output*b:one_output*(b+1)], -1)
        for i in range(indices.shape[0]):
            pred[i,one_output*b + indices[i]] = 1
    return pred

#################################### Helper functions for Single computation functions ####################################

def speedup_clip(speedup):
    if speedup<0.01:
        speedup = 0.01
    return speedup

def sched_json_to_sched_str(sched_json): # Works only for 1 comp programs
    orig_loop_nest = []
    orig_loop_nest.append(sched_json['tree_structure']['loop_name'])
    child_list = sched_json['tree_structure']['child_list']
    while len(child_list)>0:
        child_loop = child_list[0]
        orig_loop_nest.append(child_loop['loop_name'])
        child_list = child_loop['child_list']
        
    comp_name = [n for n in sched_json.keys() if not n in ['unfuse_iterators','tree_structure','execution_times']][0]
    schedule = sched_json[comp_name]
    transf_loop_nest = orig_loop_nest
    sched_str = ''
    
    if schedule['interchange_dims']:
        first_dim_index = transf_loop_nest.index(schedule['interchange_dims'][0])
        second_dim_index = transf_loop_nest.index(schedule['interchange_dims'][1])
        sched_str+='I(L'+str(first_dim_index)+',L'+str(second_dim_index)+')'
        transf_loop_nest[first_dim_index], transf_loop_nest[second_dim_index] = transf_loop_nest[second_dim_index], transf_loop_nest[first_dim_index]
    if schedule['skewing']:
        first_dim_index = transf_loop_nest.index(schedule['skewing']['skewed_dims'][0])
        second_dim_index = transf_loop_nest.index(schedule['skewing']['skewed_dims'][1])
        first_factor = schedule['skewing']['skewing_factors'][0]
        second_factor = schedule['skewing']['skewing_factors'][1]
        sched_str+='S(L'+str(first_dim_index)+',L'+str(second_dim_index)+','+str(first_factor)+','+str(second_factor)+')'
    if schedule['parallelized_dim']:
        dim_index = transf_loop_nest.index(schedule['parallelized_dim'])
        sched_str+='P(L'+str(dim_index)+')'
    if schedule['tiling']:
        if schedule['tiling']['tiling_depth']==2:
            first_dim = schedule['tiling']['tiling_dims'][0]
            second_dim = schedule['tiling']['tiling_dims'][1]
            first_dim_index = transf_loop_nest.index(first_dim)
            second_dim_index = transf_loop_nest.index(second_dim)
            first_factor = schedule['tiling']['tiling_factors'][0]
            second_factor = schedule['tiling']['tiling_factors'][1]
            sched_str+='T2(L'+str(first_dim_index)+',L'+str(second_dim_index)+','+str(first_factor)+','+str(second_factor)+')'
            i = transf_loop_nest.index(first_dim)
            transf_loop_nest[i:i+1]=first_dim+'_outer', second_dim+'_outer'
            i = transf_loop_nest.index(second_dim)
            transf_loop_nest[i:i+1]=first_dim+'_inner', second_dim+'_inner'
        else: #tiling depth == 3
            first_dim = schedule['tiling']['tiling_dims'][0]
            second_dim = schedule['tiling']['tiling_dims'][1]
            third_dim = schedule['tiling']['tiling_dims'][2]
            first_dim_index = transf_loop_nest.index(first_dim)
            second_dim_index = transf_loop_nest.index(second_dim)
            third_dim_index = transf_loop_nest.index(third_dim)
            first_factor = schedule['tiling']['tiling_factors'][0]
            second_factor = schedule['tiling']['tiling_factors'][1]
            third_factor = schedule['tiling']['tiling_factors'][2]
            sched_str+='T3(L'+str(first_dim_index)+',L'+str(second_dim_index)+',L'+str(third_dim_index)+','+str(first_factor)+','+str(second_factor)+','+str(third_factor)+')'
            i = transf_loop_nest.index(first_dim)
            transf_loop_nest[i:i+1]=first_dim+'_outer', second_dim+'_outer', third_dim+'_outer'
            i = transf_loop_nest.index(second_dim)
            transf_loop_nest[i:i+1]=first_dim+'_inner', second_dim+'_inner', third_dim+'_inner'
            transf_loop_nest.remove(third_dim)
    if schedule['unrolling_factor']:
        dim_index = len(transf_loop_nest)-1
        dim_name =transf_loop_nest[-1]
        sched_str+='U(L'+str(dim_index)+','+schedule['unrolling_factor']+')'
        transf_loop_nest[dim_index:dim_index+1] = dim_name+'_Uouter', dim_name+'_Uinner'
    
    return sched_str

#################################### Helper functions for multiple computation functions ####################################

def shared_loop_nest(program_json):
    stop = False
    loop_nest = []
    iterators = program_json['iterators']
    j = 0
    for i in list(iterators.keys()) : 
        if not stop : 
            iterator = iterators[i]
            if iterator['child_iterators'] != [] :  #in the middle of the tree
                if iterator['computations_list'] != [] :  #has some computations
                    stop = True
                else:
                    if len( iterator['child_iterators'] ) >= 2 :
                        stop = True
            if stop == False :
                loop_nest.append( "i"+str(j) )
        else : 
            return loop_nest
        j = j + 1
    return loop_nest

def legal_LI(schedule_str, sched_ind, loops):
    
    if(schedule_str==""):
        return True
    else :
        regex1 = "I\(\{[C0-9,]+\},L[0-9]+,L[0-9]+\)$" 
        LI = re.findall(regex1, schedule_str)  # get the LI string
        
        m = re.findall(r'\d+', LI[0])  # get the loops
        L1 = 'i' + m[-2]
        L2 = 'i' + m[-1]
        if (L1 in loops) and (L2 in loops):
            return True
        else:
            return False
        