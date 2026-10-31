//! Exact per-invocation reachability over interned graph objects.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use std::collections::{HashMap, HashSet};

#[pyfunction]
fn affected_roots(
    py: Python<'_>,
    roots: Vec<Bound<'_, PyAny>>,
    changed: &Bound<'_, PyDict>,
) -> PyResult<Vec<usize>> {
    let mut cache: HashMap<usize, bool> = changed
        .keys()
        .iter()
        .map(|n| (n.as_ptr() as usize, true))
        .collect();
    if mutant_on!(py, "bindings.walk/roots_only") {
        return Ok((0..roots.len())
            .filter(|&i| cache.contains_key(&(roots[i].as_ptr() as usize)))
            .collect());
    }
    let mut out = Vec::new();
    for (i, root) in roots.into_iter().enumerate() {
        let key = root.as_ptr() as usize;
        let mut stack = vec![(root, false)];
        while let Some((node, visited)) = stack.pop() {
            let at = node.as_ptr() as usize;
            if cache.contains_key(&at) {
                continue;
            }
            let src = node.getattr("src")?.cast_into::<PyTuple>()?;
            if visited {
                cache.insert(at, src.iter().any(|s| cache[&(s.as_ptr() as usize)]));
            } else {
                stack.push((node, true));
                for s in src.iter().rev() {
                    if !cache.contains_key(&(s.as_ptr() as usize)) {
                        stack.push((s, false));
                    }
                }
            }
        }
        if cache[&key] {
            out.push(i);
        }
    }
    Ok(out)
}

#[pyfunction]
fn graph_nodes<'py>(
    roots: Vec<Bound<'py, PyAny>>,
    op: &Bound<'py, PyAny>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let mut seen = HashSet::new();
    let mut stack = roots;
    let mut out = Vec::new();
    while let Some(node) = stack.pop() {
        if !seen.insert(node.as_ptr() as usize) {
            continue;
        }
        if node.getattr("op")?.is(op) {
            out.push(node.clone());
        }
        for s in node.getattr("src")?.cast_into::<PyTuple>()?.iter() {
            stack.push(s);
        }
    }
    Ok(out)
}

/// BUFFER is an identity-parametric leaf; all other interning fields and DAG
/// edges are exact. No normalized UOp graph or live allocation is retained.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn capture_graph<'py>(
    py: Python<'py>,
    root: Bound<'py, PyAny>,
    buffer_op: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyTuple>, Vec<Bound<'py, PyAny>>, Vec<usize>)> {
    let drops_tag = mutant_on!(py, "bindings.capture/drops_tag");
    let mut indices = HashMap::new();
    let mut stack = vec![(root, false)];
    let mut nodes = Vec::new();
    let mut descriptors = Vec::new();
    let mut buffers = Vec::new();
    while let Some((node, visited)) = stack.pop() {
        let at = node.as_ptr() as usize;
        if indices.contains_key(&at) {
            continue;
        }
        let op = node.getattr("op")?;
        let is_buffer = op.is(buffer_op);
        let src = node.getattr("src")?.cast_into::<PyTuple>()?;
        if !visited && !is_buffer {
            stack.push((node, true));
            for child in src.iter().rev() {
                if !indices.contains_key(&(child.as_ptr() as usize)) {
                    stack.push((child, false));
                }
            }
            continue;
        }
        let edges = if is_buffer {
            Vec::new()
        } else {
            src.iter()
                .map(|s| indices[&(s.as_ptr() as usize)])
                .collect::<Vec<usize>>()
        };
        let shape = if is_buffer {
            buffers.push(nodes.len());
            node.getattr("device")?
        } else {
            py.None().into_bound(py)
        };
        // BUFFER identity/ParamArg numbering is excluded; extent and device stay exact.
        let arg = if is_buffer {
            node.call_method0("max_numel")?
        } else {
            node.getattr("arg")?
        };
        descriptors.push(PyTuple::new(
            py,
            [
                op,
                node.getattr("dtype")?,
                arg,
                if drops_tag {
                    py.None().into_bound(py)
                } else {
                    node.getattr("tag")?
                },
                shape,
                PyTuple::new(py, edges)?.into_any(),
            ],
        )?);
        indices.insert(at, nodes.len());
        nodes.push(node);
    }
    Ok((PyTuple::new(py, descriptors)?, nodes, buffers))
}

enum Edge {
    Static(Py<PyAny>),
    Dynamic(usize),
}
struct Build {
    node: Py<PyAny>,
    edges: Vec<Edge>,
}

#[pyclass(module = "kohakutpu.compiler._rs")]
struct CaptureTemplate {
    builds: Vec<Build>,
    root: Edge,
    parameters: usize,
}

#[pymethods]
impl CaptureTemplate {
    #[new]
    fn new(root: Bound<'_, PyAny>, parameters: Vec<Bound<'_, PyAny>>) -> PyResult<Self> {
        let count = parameters.len();
        let mut dynamic: HashMap<usize, usize> = parameters
            .iter()
            .enumerate()
            .map(|(i, n)| (n.as_ptr() as usize, i))
            .collect();
        let mut seen: HashSet<usize> = dynamic.keys().copied().collect();
        let mut stack = vec![(root.clone(), false)];
        let mut builds = Vec::new();
        while let Some((node, visited)) = stack.pop() {
            let at = node.as_ptr() as usize;
            if seen.contains(&at) {
                continue;
            }
            let src = node.getattr("src")?.cast_into::<PyTuple>()?;
            if !visited {
                stack.push((node, true));
                for child in src.iter().rev() {
                    stack.push((child, false));
                }
                continue;
            }
            seen.insert(at);
            if src
                .iter()
                .any(|s| dynamic.contains_key(&(s.as_ptr() as usize)))
            {
                let edges = src
                    .iter()
                    .map(|s| match dynamic.get(&(s.as_ptr() as usize)) {
                        Some(&index) => Edge::Dynamic(index),
                        None => Edge::Static(s.unbind()),
                    })
                    .collect();
                dynamic.insert(at, count + builds.len());
                builds.push(Build {
                    node: node.unbind(),
                    edges,
                });
            }
        }
        let root = match dynamic.get(&(root.as_ptr() as usize)) {
            Some(&index) => Edge::Dynamic(index),
            None => Edge::Static(root.unbind()),
        };
        Ok(Self {
            builds,
            root,
            parameters: count,
        })
    }

    fn instantiate<'py>(
        &self,
        py: Python<'py>,
        mut values: Vec<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if values.len() != self.parameters {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "capture template parameter count",
            ));
        }
        for build in &self.builds {
            let src = PyTuple::new(
                py,
                build.edges.iter().map(|e| match e {
                    Edge::Dynamic(i) => values[*i].clone(),
                    Edge::Static(n) => n.bind(py).clone(),
                }),
            )?;
            let node = build.node.bind(py);
            let value = node.get_type().call1((
                node.getattr("op")?,
                node.getattr("dtype")?,
                src,
                node.getattr("arg")?,
                node.getattr("tag")?,
            ))?;
            values.push(value);
        }
        Ok(match &self.root {
            Edge::Dynamic(i) => values[*i].clone(),
            Edge::Static(n) => n.bind(py).clone(),
        })
    }
}

#[pyfunction]
fn rewrite_roots<'py>(
    py: Python<'py>,
    roots: Vec<Bound<'py, PyAny>>,
    changed: &Bound<'py, PyDict>,
    function_op: &Bound<'py, PyAny>,
    call_op: &Bound<'py, PyAny>,
    opaque: &Bound<'py, PyAny>,
    rebuild_dtype: &Bound<'py, PyAny>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let mut replaced: HashMap<usize, Bound<'py, PyAny>> = HashMap::new();
    let mut stack: Vec<_> = roots.iter().rev().map(|n| (n.clone(), false)).collect();
    while let Some((node, visited)) = stack.pop() {
        let at = node.as_ptr() as usize;
        if replaced.contains_key(&at) {
            continue;
        }
        if !visited {
            if let Some(value) = changed.get_item(&node)? {
                if !value.is(&node) {
                    replaced.insert(at, value);
                    continue;
                }
            }
        }
        let src = node.getattr("src")?.cast_into::<PyTuple>()?;
        if !visited {
            let op = node.getattr("op")?;
            if op.is(function_op)
                || (op.is(call_op) && opaque.contains(src.get_item(0)?.getattr("op")?)?)
            {
                let body = src.get_item(0)?;
                replaced.insert(body.as_ptr() as usize, body);
            }
            stack.push((node, true));
            for child in src.iter().rev() {
                if !replaced.contains_key(&(child.as_ptr() as usize)) {
                    stack.push((child, false));
                }
            }
        } else {
            let different = src.iter().any(|s| !replaced[&(s.as_ptr() as usize)].is(&s));
            let result = if different {
                let new_src = PyTuple::new(
                    py,
                    src.iter().map(|s| replaced[&(s.as_ptr() as usize)].clone()),
                )?;
                let dtype = rebuild_dtype.call1((&node, &new_src))?;
                node.get_type().call1((
                    node.getattr("op")?,
                    dtype,
                    new_src,
                    node.getattr("arg")?,
                    node.getattr("tag")?,
                ))?
            } else {
                node
            };
            replaced.insert(at, result);
        }
    }
    Ok(roots
        .iter()
        .map(|n| replaced[&(n.as_ptr() as usize)].clone())
        .collect())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rewrite_roots, m)?)?;
    m.add_class::<CaptureTemplate>()?;
    m.add_function(wrap_pyfunction!(capture_graph, m)?)?;
    m.add_function(wrap_pyfunction!(affected_roots, m)?)?;
    m.add_function(wrap_pyfunction!(graph_nodes, m)?)
}
