#ifndef _TIRAMISU_AUTO_SCHEDULER_DNN_ACCESSES_
#define _TIRAMISU_AUTO_SCHEDULER_DNN_ACCESSES_

#include <tiramisu/core.h>
#include <tiramisu/expr.h>

namespace tiramisu::auto_scheduler
{

class dnn_iterator
{
public:
    int low_bound;
    int up_bound;
    
    dnn_iterator(int low_bound, int up_bound)
        : low_bound(low_bound), up_bound(up_bound) {}
};

class dnn_schedule
{
public:
    int nb_iterators;

    std::vector<bool> interchanged;
    std::vector<bool> tiled;
    std::vector<int> tiling_fact;
    int unrolling_fact = 0;
    
    dnn_schedule(int nb_iterators) 
        : nb_iterators(nb_iterators), interchanged(nb_iterators, false),
          tiled(nb_iterators, false), tiling_fact(nb_iterators, 0),
          unrolling_fact(0) {}
};

class dnn_access_matrix
{
protected:
    void fill_matrix_row(int i, tiramisu::expr const& e, bool minus = false);

public:
    int nb_iterators;
    int nb_dims;
    std::vector<std::vector<int>> matrix;
    
    std::string buffer_name;
    int buffer_id;
    
    tiramisu::computation *comp;
    
    dnn_access_matrix(int nb_iterators, int nb_dims);
    dnn_access_matrix(int nb_iterators, tiramisu::expr const& e, tiramisu::computation *comp);
    
    void set_buffer_id(tiramisu::function *fct);
};

class dnn_accesses
{
public:
    tiramisu::computation* comp;
    int nb_iterators;
    std::vector<dnn_access_matrix> accesses_list;
        
    dnn_accesses(tiramisu::computation *comp, int nb_iterators)
        : comp(comp), nb_iterators(nb_iterators)
    {
        create_accesses(comp->get_expr());
    }
        
    void create_accesses(tiramisu::expr const& e);
};

}

#endif
