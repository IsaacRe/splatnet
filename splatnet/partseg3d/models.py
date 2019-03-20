"""
Copyright (C) 2018 NVIDIA Corporation.  All rights reserved.
Licensed under the CC BY-NC-SA 4.0 license (https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
"""
from functools import reduce
import caffe
from caffe import layers as L, params as P
from splatnet.utils import get_prototxt, parse_channel_scale, map_channel_scale


def partseg_seq(arch_str='64_128_256_256', batchnorm=True,
                skip_str=(),  # tuple of strings like '4_1_ga' - relu4 <- relu1 w/ options 'ga'
                bilateral_nbr=1,
                conv_weight_filler='xavier', bltr_weight_filler='gauss_0.001',
                dataset='shapenet', dataset_params=None, category='airplane',
                sample_size=3000, batch_size=32,
                feat_dims_str='x_y_z', lattice_dims_str=None,
                deploy=False, create_prototxt=True, save_path=None):

    n = caffe.NetSpec()

    arch_str = [(v[0], int(v[1:])) if v[0] in {'b', 'c'} else ('c', int(v)) for v in arch_str.split('_')]
    num_bltr_layers = sum(v[0] == 'b' for v in arch_str)

    if num_bltr_layers > 0:
        if type(lattice_dims_str) == str:
            lattice_dims_str = (lattice_dims_str,) * num_bltr_layers
        elif len(lattice_dims_str) == 1:
            lattice_dims_str = lattice_dims_str * num_bltr_layers
        else:
            assert len(lattice_dims_str) == num_bltr_layers, '{} lattices should be provided'.format(num_bltr_layers)
        feat_dims = parse_channel_scale(feat_dims_str, channel_str=True)[0]
        lattice_dims = [parse_channel_scale(s, channel_str=True)[0] for s in lattice_dims_str]
        input_dims_w_dup = feat_dims + reduce(lambda x, y: x + y, lattice_dims)
        input_dims = reduce(lambda x, y: x if y in x else x + [y], input_dims_w_dup, [])
        feat_dims_str = map_channel_scale(feat_dims_str, input_dims)
        lattice_dims_str = [map_channel_scale(s, input_dims) for s in lattice_dims_str]
        input_dims_str = '_'.join(input_dims)
    else:
        feat_dims = parse_channel_scale(feat_dims_str, channel_str=True)[0]
        input_dims = feat_dims
        feat_dims_str = map_channel_scale(feat_dims_str, input_dims)
        input_dims_str = '_'.join(input_dims)

    # dataset specific settings: nclass, datalayer_train, datalayer_test
    if dataset == 'shapenet':
        from splatnet.configs import SN_CATEGORIES, SN_CATEGORY_NAMES, SN_NUM_PART_CATEGORIES

        if not category.startswith('0'):
            category = SN_CATEGORIES[SN_CATEGORY_NAMES.index(category)]

        nclass = SN_NUM_PART_CATEGORIES[SN_CATEGORIES.index(category)]

        # default dataset params
        dataset_params_new = {} if not dataset_params else dataset_params
        dataset_params = dict(subset_train='train', subset_test='val')
        dataset_params.update(dataset_params_new)

        dataset_params['feat_dims'] = input_dims_str
        dataset_params['sample_size'] = sample_size
        dataset_params['batch_size'] = batch_size
        dataset_params['category'] = category

        # dataset params type casting
        for v in {'jitter_xyz', 'jitter_rotation', 'jitter_stretch'}:
            if v in dataset_params:
                dataset_params[v] = float(dataset_params[v])
        for v in {'sample_size', 'batch_size'}:
            if v in dataset_params:
                dataset_params[v] = int(dataset_params[v])

        # training time dataset params
        dataset_params_train = dataset_params.copy()
        dataset_params_train['subset'] = dataset_params['subset_train']
        del dataset_params_train['subset_train'], dataset_params_train['subset_test']

        # testing time dataset params: turn off all data augmentations
        dataset_params_test = dataset_params.copy()
        dataset_params_test['subset'] = dataset_params['subset_test']
        dataset_params_test['jitter_xyz'] = 0.0
        dataset_params_test['jitter_stretch'] = 0.0
        dataset_params_test['jitter_rotation'] = 0.0
        del dataset_params_test['subset_train'], dataset_params_test['subset_test']

        # data layers
        datalayer_train = L.Python(name='data', include=dict(phase=caffe.TRAIN), ntop=3,
                                   python_param=dict(module='dataset_shapenet', layer='InputShapenet',
                                                     param_str=repr(dataset_params_train)))
        datalayer_test = L.Python(name='data', include=dict(phase=caffe.TEST), ntop=0, top=['data', 'label'],
                                  python_param=dict(module='dataset_shapenet', layer='InputShapenet',
                                                    param_str=repr(dataset_params_test)))
    else:
        raise ValueError('Dataset {} unknown'.format(dataset))

    # Input/Data layer
    if deploy:
        n.data = L.Input(shape=dict(dim=[1, len(input_dims), 1, sample_size]))
    else:
        n.data, n.label = datalayer_train
        n.test_data = datalayer_test
    n.data_feat = L.Python(n.data, python_param=dict(module='custom_layers', layer='PickAndScale',
                                                     param_str=feat_dims_str))
    top_prev = n.data_feat

    if conv_weight_filler in {'xavier', 'msra'}:
        conv_weight_filler = dict(type=conv_weight_filler)
    elif conv_weight_filler.startswith('gauss_'):
        conv_weight_filler = dict(type='gaussian', std=float(conv_weight_filler.split('_')[1]))
    else:
        conv_weight_filler = eval(conv_weight_filler)
    assert bltr_weight_filler.startswith('gauss_')
    bltr_weight_filler = dict(type='gaussian', std=float(bltr_weight_filler.split('_')[1]))

    # multiple 1x1 conv-(bn)-relu blocks, optionally with a single global pooling somewhere among them

    idx = 1
    bltr_idx = 0
    lattices = dict()
    last_in_block = dict()
    for (layer_type, n_out) in arch_str:
        if layer_type == 'c':
            n['conv' + str(idx)] = L.Convolution(top_prev,
                                                 convolution_param=dict(num_output=n_out,
                                                                        kernel_size=1, stride=1, pad=0,
                                                                        weight_filler=conv_weight_filler,
                                                                        bias_filler=dict(type='constant', value=0)),
                                                 param=[dict(lr_mult=1), dict(lr_mult=0.1)])
        elif layer_type == 'b':
            lattice_dims_str_curr = lattice_dims_str[bltr_idx]
            if lattice_dims_str_curr in lattices:
                top_data_lattice, top_lattice = lattices[lattice_dims_str_curr]
                n['conv' + str(idx)] = L.Permutohedral(top_prev, top_data_lattice, top_data_lattice, top_lattice,
                                                       permutohedral_param=dict(num_output=n_out,
                                                                                group=1,
                                                                                neighborhood_size=bilateral_nbr,
                                                                                bias_term=True,
                                                                                norm_type=P.Permutohedral.AFTER,
                                                                                offset_type=P.Permutohedral.NONE,
                                                                                filter_filler=bltr_weight_filler,
                                                                                bias_filler=dict(type='constant',
                                                                                                 value=0)),
                                                       param=[{'lr_mult': 1, 'decay_mult': 1},
                                                              {'lr_mult': 2, 'decay_mult': 0}])
            else:
                top_data_lattice = L.Python(n.data, python_param=dict(module='custom_layers', layer='PickAndScale',
                                                                      param_str=lattice_dims_str_curr))
                n['data_lattice' + str(len(lattices))] = top_data_lattice
                if lattice_dims_str.count(lattice_dims_str_curr) > 1:
                    n['conv' + str(idx)], top_lattice = L.Permutohedral(top_prev, top_data_lattice, top_data_lattice,
                                                                        ntop=2,
                                                                        permutohedral_param=dict(
                                                                            num_output=n_out,
                                                                            group=1,
                                                                            neighborhood_size=bilateral_nbr,
                                                                            bias_term=True,
                                                                            norm_type=P.Permutohedral.AFTER,
                                                                            offset_type=P.Permutohedral.NONE,
                                                                            filter_filler=bltr_weight_filler,
                                                                            bias_filler=dict(type='constant',
                                                                                             value=0)),
                                                                        param=[{'lr_mult': 1, 'decay_mult': 1},
                                                                               {'lr_mult': 2, 'decay_mult': 0}])
                    n['lattice' + str(len(lattices))] = top_lattice
                else:
                    n['conv' + str(idx)] = L.Permutohedral(top_prev, top_data_lattice, top_data_lattice,
                                                           permutohedral_param=dict(
                                                               num_output=n_out,
                                                               group=1,
                                                               neighborhood_size=bilateral_nbr,
                                                               bias_term=True,
                                                               norm_type=P.Permutohedral.AFTER,
                                                               offset_type=P.Permutohedral.NONE,
                                                               filter_filler=bltr_weight_filler,
                                                               bias_filler=dict(type='constant', value=0)),
                                                           param=[{'lr_mult': 1, 'decay_mult': 1},
                                                                  {'lr_mult': 2, 'decay_mult': 0}])
                    top_lattice = None

                lattices[lattice_dims_str_curr] = (top_data_lattice, top_lattice)

            bltr_idx += 1

        top_prev = n['conv' + str(idx)]
        if batchnorm:
            n['bn'+str(idx)] = L.BatchNorm(top_prev)
            top_prev = n['bn'+str(idx)]
        n['relu'+str(idx)] = L.ReLU(top_prev, in_place=True)
        top_prev = n['relu'+str(idx)]

        # skip connection & global pooling
        if skip_str is None:
            skip_str = ()
        skip_tos = [v.split('_')[0] for v in skip_str]
        if str(idx) in skip_tos:
            skip_idxs = list(filter(lambda i: skip_tos[i] == str(idx), range(len(skip_tos))))
            skip_params = [skip_str[i].split('_') for i in skip_idxs]
            if len(skip_params[0]) == 2:
                assert all(len(v) == 2 for v in skip_params)
            else:
                assert all(v[2] == skip_params[0][2] for v in skip_params)

            if len(skip_params[0]) > 2 and 'g' in skip_params[0][2]:  # global pooling on current layer
                n['gpool'+str(idx)] = L.Python(top_prev,
                                               python_param=dict(module='custom_layers', layer='GlobalPooling'))
                top_prev = n['gpool'+str(idx)]

            if len(skip_params[0]) > 2 and 'a' in skip_params[0][2]:  # addition instead of concatenation
                n['add'+str(idx)] = L.Eltwise(top_prev, *[last_in_block[int(v[1])] for v in skip_params],
                                              eltwise_param=dict(operation=P.Eltwise.SUM))
                top_prev = n['add'+str(idx)]
            else:
                n['concat'+str(idx)] = L.Concat(top_prev, *[last_in_block[int(v[1])] for v in skip_params])
                top_prev = n['concat'+str(idx)]

        last_in_block[idx] = top_prev
        idx += 1

    # classification & loss
    n['conv'+str(idx)] = L.Convolution(top_prev,
                                       convolution_param=dict(num_output=nclass, kernel_size=1, stride=1, pad=0,
                                                              weight_filler=conv_weight_filler,
                                                              bias_filler=dict(type='constant', value=0)),
                                       param=[dict(lr_mult=1), dict(lr_mult=0.1)])
    top_prev = n['conv'+str(idx)]

    if deploy:
        n.prob = L.Softmax(top_prev)
    else:
        n.loss = L.SoftmaxWithLoss(top_prev, n.label)
        n.accuracy = L.Accuracy(top_prev, n.label)

    net = n.to_proto()

    if create_prototxt:
        net = get_prototxt(net, save_path)

    return net


def partseg_seq_combined_categories(arch_str='64_128_256_256', batchnorm=True,
                                    skip_str=(),  # tuple of strings like '4_1_ga' - relu4 <- relu1 w/ options 'ga'
                                    bilateral_nbr=1,
                                    conv_weight_filler='xavier', bltr_weight_filler='gauss_0.001',
                                    dataset='shapenet', dataset_params=None,
                                    sample_size=3000, batch_size=32,
                                    feat_dims_str='x_y_z', lattice_dims_str=None,
                                    renorm_class=False,
                                    deploy=False, create_prototxt=True, save_path=None):

    n = caffe.NetSpec()

    arch_str = [(v[0], int(v[1:])) if v[0] in {'b', 'c'} else ('c', int(v)) for v in arch_str.split('_')]
    num_bltr_layers = sum(v[0] == 'b' for v in arch_str)

    if num_bltr_layers > 0:
        if type(lattice_dims_str) == str:
            lattice_dims_str = (lattice_dims_str,) * num_bltr_layers
        elif len(lattice_dims_str) == 1:
            lattice_dims_str = lattice_dims_str * num_bltr_layers
        else:
            assert len(lattice_dims_str) == num_bltr_layers, '{} lattices should be provided'.format(num_bltr_layers)
        feat_dims = parse_channel_scale(feat_dims_str, channel_str=True)[0]
        lattice_dims = [parse_channel_scale(s, channel_str=True)[0] for s in lattice_dims_str]
        input_dims_w_dup = feat_dims + reduce(lambda x, y: x + y, lattice_dims)
        input_dims = reduce(lambda x, y: x if y in x else x + [y], input_dims_w_dup, [])
        feat_dims_str = map_channel_scale(feat_dims_str, input_dims)
        lattice_dims_str = [map_channel_scale(s, input_dims) for s in lattice_dims_str]
        input_dims_str = '_'.join(input_dims)
    else:
        feat_dims = parse_channel_scale(feat_dims_str, channel_str=True)[0]
        input_dims = feat_dims
        feat_dims_str = map_channel_scale(feat_dims_str, input_dims)
        input_dims_str = '_'.join(input_dims)

    # dataset specific settings: nclass, datalayer_train, datalayer_test
    if dataset == 'shapenet':
        from splatnet.configs import SN_NUM_PART_CATEGORIES

        nclass = sum(SN_NUM_PART_CATEGORIES)

        # default dataset params
        dataset_params_new = {} if not dataset_params else dataset_params
        dataset_params = dict(subset_train='train', subset_test='val')
        dataset_params.update(dataset_params_new)

        dataset_params['feat_dims'] = input_dims_str
        dataset_params['sample_size'] = sample_size
        dataset_params['batch_size'] = batch_size
        dataset_params['output_mask'] = renorm_class

        # dataset params type casting
        for v in {'jitter_xyz', 'jitter_rotation', 'jitter_stretch'}:
            if v in dataset_params:
                dataset_params[v] = float(dataset_params[v])
        for v in {'sample_size', 'batch_size'}:
            if v in dataset_params:
                dataset_params[v] = int(dataset_params[v])
        for v in {'output_mask'}:
            if v in dataset_params:
                dataset_params[v] = bool(dataset_params[v])

        # training time dataset params
        dataset_params_train = dataset_params.copy()
        dataset_params_train['subset'] = dataset_params['subset_train']
        del dataset_params_train['subset_train'], dataset_params_train['subset_test']

        # testing time dataset params: turn off all data augmentations
        dataset_params_test = dataset_params.copy()
        dataset_params_test['subset'] = dataset_params['subset_test']
        dataset_params_test['jitter_xyz'] = 0.0
        dataset_params_test['jitter_stretch'] = 0.0
        dataset_params_test['jitter_rotation'] = 0.0
        del dataset_params_test['subset_train'], dataset_params_test['subset_test']

        # data layers
        datalayer_train = L.Python(name='data', include=dict(phase=caffe.TRAIN),
                                   ntop=4 if renorm_class else 3,
                                   python_param=dict(module='dataset_shapenet', layer='InputShapenetAllCategories',
                                                     param_str=repr(dataset_params_train)))
        datalayer_test = L.Python(name='data', include=dict(phase=caffe.TEST), ntop=0,
                                  top=['data', 'label', 'label_mask'] \
                                          if renorm_class else ['data', 'label'],
                                  python_param=dict(module='dataset_shapenet', layer='InputShapenetAllCategories',
                                                    param_str=repr(dataset_params_test)))
    else:
        raise ValueError('Dataset {} unknown'.format(dataset))

    # Input/Data layer
    if deploy:
        n.data = L.Input(shape=dict(dim=[1, len(input_dims), 1, sample_size]))
        if renorm_class:
            n.label_mask = L.Input(shape=dict(dim=[1, nclass, 1, 1]))
    else:
        if renorm_class:
            n.data, n.label, n.category_labels, n.label_mask = datalayer_train
        else:
            n.data, n.label, n.category_labels = datalayer_train
        n.test_data = datalayer_test
    n.data_feat = L.Python(n.data, python_param=dict(module='custom_layers', layer='PickAndScale',
                                                     param_str=feat_dims_str))
    top_prev = n.data_feat

    if conv_weight_filler in {'xavier', 'msra'}:
        conv_weight_filler = dict(type=conv_weight_filler)
    elif conv_weight_filler.startswith('gauss_'):
        conv_weight_filler = dict(type='gaussian', std=float(conv_weight_filler.split('_')[1]))
    else:
        conv_weight_filler = eval(conv_weight_filler)
    assert bltr_weight_filler.startswith('gauss_')
    bltr_weight_filler = dict(type='gaussian', std=float(bltr_weight_filler.split('_')[1]))

    # multiple 1x1 conv-(bn)-relu blocks, optionally with a single global pooling somewhere among them

    idx = 1
    bltr_idx = 0
    lattices = dict()
    last_in_block = dict()
    for (layer_type, n_out) in arch_str:
        if layer_type == 'c':
            n['conv' + str(idx)] = L.Convolution(top_prev,
                                                 convolution_param=dict(num_output=n_out,
                                                                        kernel_size=1, stride=1, pad=0,
                                                                        weight_filler=conv_weight_filler,
                                                                        bias_filler=dict(type='constant', value=0)),
                                                 param=[dict(lr_mult=1), dict(lr_mult=0.1)])
        elif layer_type == 'b':
            lattice_dims_str_curr = lattice_dims_str[bltr_idx]
            if lattice_dims_str_curr in lattices:
                top_data_lattice, top_lattice = lattices[lattice_dims_str_curr]
                n['conv' + str(idx)] = L.Permutohedral(top_prev, top_data_lattice, top_data_lattice, top_lattice,
                                                       permutohedral_param=dict(num_output=n_out,
                                                                                group=1,
                                                                                neighborhood_size=bilateral_nbr,
                                                                                bias_term=True,
                                                                                norm_type=P.Permutohedral.AFTER,
                                                                                offset_type=P.Permutohedral.NONE,
                                                                                filter_filler=bltr_weight_filler,
                                                                                bias_filler=dict(type='constant',
                                                                                                 value=0)),
                                                       param=[{'lr_mult': 1, 'decay_mult': 1},
                                                              {'lr_mult': 2, 'decay_mult': 0}])
            else:
                top_data_lattice = L.Python(n.data, python_param=dict(module='custom_layers', layer='PickAndScale',
                                                                      param_str=lattice_dims_str_curr))
                n['data_lattice' + str(len(lattices))] = top_data_lattice
                if lattice_dims_str.count(lattice_dims_str_curr) > 1:
                    n['conv' + str(idx)], top_lattice = L.Permutohedral(top_prev, top_data_lattice, top_data_lattice,
                                                                        ntop=2,
                                                                        permutohedral_param=dict(
                                                                            num_output=n_out,
                                                                            group=1,
                                                                            neighborhood_size=bilateral_nbr,
                                                                            bias_term=True,
                                                                            norm_type=P.Permutohedral.AFTER,
                                                                            offset_type=P.Permutohedral.NONE,
                                                                            filter_filler=bltr_weight_filler,
                                                                            bias_filler=dict(type='constant',
                                                                                             value=0)),
                                                                        param=[{'lr_mult': 1, 'decay_mult': 1},
                                                                               {'lr_mult': 2, 'decay_mult': 0}])
                    n['lattice' + str(len(lattices))] = top_lattice
                else:
                    n['conv' + str(idx)] = L.Permutohedral(top_prev, top_data_lattice, top_data_lattice,
                                                           permutohedral_param=dict(
                                                               num_output=n_out,
                                                               group=1,
                                                               neighborhood_size=bilateral_nbr,
                                                               bias_term=True,
                                                               norm_type=P.Permutohedral.AFTER,
                                                               offset_type=P.Permutohedral.NONE,
                                                               filter_filler=bltr_weight_filler,
                                                               bias_filler=dict(type='constant', value=0)),
                                                           param=[{'lr_mult': 1, 'decay_mult': 1},
                                                                  {'lr_mult': 2, 'decay_mult': 0}])
                    top_lattice = None

                lattices[lattice_dims_str_curr] = (top_data_lattice, top_lattice)

            bltr_idx += 1

        top_prev = n['conv' + str(idx)]
        if batchnorm:
            n['bn'+str(idx)] = L.BatchNorm(top_prev)
            top_prev = n['bn'+str(idx)]
        n['relu'+str(idx)] = L.ReLU(top_prev, in_place=True)
        top_prev = n['relu'+str(idx)]

        # skip connection & global pooling
        if skip_str is None:
            skip_str = ()
        skip_tos = [v.split('_')[0] for v in skip_str]
        if str(idx) in skip_tos:
            skip_idxs = list(filter(lambda i: skip_tos[i] == str(idx), range(len(skip_tos))))
            skip_params = [skip_str[i].split('_') for i in skip_idxs]
            if len(skip_params[0]) == 2:
                assert all(len(v) == 2 for v in skip_params)
            else:
                assert all(v[2] == skip_params[0][2] for v in skip_params)

            if len(skip_params[0]) > 2 and 'g' in skip_params[0][2]:  # global pooling on current layer
                n['gpool'+str(idx)] = L.Python(top_prev,
                                               python_param=dict(module='custom_layers', layer='GlobalPooling'))
                top_prev = n['gpool'+str(idx)]

            if len(skip_params[0]) > 2 and 'a' in skip_params[0][2]:  # addition instead of concatenation
                n['add'+str(idx)] = L.Eltwise(top_prev, *[last_in_block[int(v[1])] for v in skip_params],
                                              eltwise_param=dict(operation=P.Eltwise.SUM))
                top_prev = n['add'+str(idx)]
            else:
                n['concat'+str(idx)] = L.Concat(top_prev, *[last_in_block[int(v[1])] for v in skip_params])
                top_prev = n['concat'+str(idx)]

        last_in_block[idx] = top_prev
        idx += 1

    # classification & loss
    n['conv'+str(idx)] = L.Convolution(top_prev,
                                       convolution_param=dict(num_output=nclass, kernel_size=1, stride=1, pad=0,
                                                              weight_filler=conv_weight_filler,
                                                              bias_filler=dict(type='constant', value=0)),
                                       param=[dict(lr_mult=1), dict(lr_mult=0.1)])
    top_prev = n['conv'+str(idx)]

    # Use Global Max Pooling to get object classifiction
    n['gpool_final'] = L.Python(top_prev,
                                python_param=dict(module='custom_layers', layer='GlobalPooling'))
    top_prev = n['gpool_final']

    renorm_class = False
    if renorm_class:
        if deploy:
            n.prob = L.Softmax(top_prev)
        else:
            n.prob_raw = L.Softmax(top_prev)
            n.prob = L.Python(n.prob_raw, n.label_mask, python_param=dict(module='custom_layers', layer='ProbRenorm'))
            n.loss = L.Python(n.prob, n.label, python_param=dict(module='custom_layers', layer='LogLoss'), loss_weight=1)
            n.accuracy = L.Accuracy(n.prob, n.label)
    else:
        if deploy:
            n.prob = L.Softmax(top_prev)
        else:
            n.loss = L.SoftmaxWithLoss(top_prev, n.category_labels)
            #n.accuracy = L.Accuracy(top_prev, n.category_labels)

    net = n.to_proto()

    if create_prototxt:
        net = get_prototxt(net, save_path)

    return net

