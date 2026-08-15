import torch


def init_weights(m, mean=0.0, std=0.01):
    classname = m.__class__.__name__
    if classname.find("Conv") != -1:
        m.weight.data.normal_(mean, std)


def get_padding(kernel_size, dilation=1):
    return int((kernel_size * dilation - dilation) / 2)


@torch.jit.script
def fused_add_tanh_sigmoid_multiply(input_a, input_b):
    in_act = input_a + input_b
    t_act_raw, s_act_raw = torch.chunk(in_act, 2, dim=1)
    t_act = torch.tanh(t_act_raw)
    s_act = torch.sigmoid(s_act_raw)
    acts = t_act * s_act
    return acts


def convert_pad_shape(pad_shape):
    l = pad_shape[::-1]
    pad_shape = [item for sublist in l for item in sublist]
    return pad_shape