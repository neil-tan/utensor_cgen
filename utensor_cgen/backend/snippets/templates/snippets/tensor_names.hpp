#ifndef _{{header_guard}}
#define _{{header_guard}}
{% if num_tensors <= 256 %}
//typedef uchar TName;
{% else %}
//typedef ushort TName;
{% endif %}


{% for tensor_macro_name, tensor_index in tensor_list.items() %}
#define {{tensor_macro_name}} {{tensor_index}}
{% endfor %}


#endif // _{{header_guard}}
