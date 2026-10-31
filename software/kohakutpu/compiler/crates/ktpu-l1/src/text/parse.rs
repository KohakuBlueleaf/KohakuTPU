//! Text to `Stmt` trees (L1.TXT.001-.015). The line splitter owns layout --
//! indentation (4 columns a tab), comments, newlines inside brackets -- and
//! the pest grammar (`ktpu.pest`) reads each logical line.

use pest::error::LineColLocation;

use super::term::Stmt;
use crate::error::{Error, Result};
use crate::mutant::on;

/// The grammar: `ktpu.pest` with `unary.pest`'s `unary`.
mod grammar {
    use pest::error::LineColLocation;
    use pest::iterators::Pair;
    use pest::Parser;

    use super::super::term::{IntStyle, Stmt, Term};
    use crate::mutant::on;

    #[derive(pest_derive::Parser)]
    #[grammar = "text/ktpu.pest"]
    #[grammar = "text/unary.pest"]
    struct Ktpu;

    include!("convert.rs");
}

/// The mutant `l1.text/placed-first`'s grammar: `unary_v0.pest`'s `unary`.
#[cfg(feature = "mutants")]
mod grammar_v0 {
    use pest::error::LineColLocation;
    use pest::iterators::Pair;
    use pest::Parser;

    use super::super::term::{IntStyle, Stmt, Term};
    use crate::mutant::on;

    #[derive(pest_derive::Parser)]
    #[grammar = "text/ktpu.pest"]
    #[grammar = "text/unary_v0.pest"]
    struct Ktpu;

    include!("convert.rs");
}

/// Whether every line is read by both grammars, any difference a panic.
#[cfg(feature = "mutants")]
static BOTH: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// From now on in this process, every line is read by both grammars (the
/// mutant's `unary` the reference) and a statement or a stop position that
/// differs panics: the witness that `unary.pest` reads what `unary_v0.pest`
/// reads.
#[cfg(feature = "mutants")]
pub fn read_both() {
    BOTH.store(true, std::sync::atomic::Ordering::Relaxed);
}

/// One logical line's statement as the active grammar reads it.
fn read(text: &str) -> std::result::Result<Stmt, LineColLocation> {
    #[cfg(feature = "mutants")]
    {
        if on("l1.text/placed-first") {
            return grammar_v0::read(text);
        }
        if BOTH.load(std::sync::atomic::Ordering::Relaxed) {
            let (got, want) = (grammar::read(text), grammar_v0::read(text));
            assert_eq!(format!("{got:?}"), format!("{want:?}"), "{text}");
            return got;
        }
    }
    grammar::read(text)
}

const TXT_ERR: &str = "L1.TXT.014";
const TXT_DEDENT: &str = "L1.TXT.015";
const TAB: usize = 4;

/// One logical line: its text (newlines inside brackets and comments made
/// spaces), where its first token is, and its indentation.
struct Logical {
    text: String,
    line: u32,
    col: u32,
    indent: usize,
    /// Whether an end of line came before it (the first statement of a text
    /// that starts at its first line has none: its indentation is ignored).
    after_nl: bool,
}

/// The logical lines of `source`, blank and comment-only lines dropped.
fn logical_lines(source: &str) -> Vec<Logical> {
    let lines: Vec<&str> = source.split('\n').collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < lines.len() {
        let phys = lines[i].strip_suffix('\r').unwrap_or(lines[i]);
        let lead: String = phys
            .chars()
            .take_while(|c| *c == ' ' || *c == '\t')
            .collect();
        let rest = phys[lead.len()..].trim_start_matches([' ', '\t', '\x0C']);
        if rest.is_empty() || rest.starts_with('#') {
            i += 1;
            continue;
        }
        let indent = lead.chars().map(|c| if c == '\t' { TAB } else { 1 }).sum();
        let col = phys.chars().count() - rest.chars().count() + 1;
        let start = i;
        let mut text = String::new();
        let mut depth: i32 = 0;
        loop {
            let phys = lines[i].strip_suffix('\r').unwrap_or(lines[i]);
            let mut in_str = false;
            let mut chars = phys.chars();
            while let Some(c) = chars.next() {
                if in_str {
                    in_str = c != '"';
                    text.push(c);
                    continue;
                }
                let paren = !on("l1.text/newline-in-brackets");
                match c {
                    '"' => in_str = true,
                    '#' if !on("l1.text/no-comment") => {
                        // A comment runs to the end of the line.
                        text.push(' ');
                        text.extend(chars.by_ref().map(|_| ' '));
                        break;
                    }
                    '{' => depth += 1,
                    '}' => depth = (depth - 1).max(0),
                    '(' | '[' if paren => depth += 1,
                    ')' | ']' if paren => depth = (depth - 1).max(0),
                    _ => {}
                }
                text.push(c);
            }
            i += 1;
            if depth == 0 || i >= lines.len() {
                break;
            }
            text.push(' ');
        }
        out.push(Logical {
            text,
            line: start as u32 + 1,
            col: col as u32,
            indent,
            after_nl: start > 0,
        });
    }
    out
}

enum Tok {
    Line(usize),
    Indent(usize),
    Dedent,
}

/// `Stmt` trees for `text`; an error names the line and column (L1.TXT.014).
pub fn parse(text: &str, file: &str) -> Result<Vec<Stmt>> {
    let source = if text.ends_with('\n') {
        text.to_string()
    } else {
        format!("{text}\n")
    };
    let lines = logical_lines(&source);
    let mut toks = Vec::new();
    let mut stack = vec![0usize];
    for (k, l) in lines.iter().enumerate() {
        if l.after_nl && !on("l1.text/flat-indent") {
            let top = *stack.last().unwrap();
            if l.indent > top {
                stack.push(l.indent);
                toks.push(Tok::Indent(k));
            } else if l.indent < top {
                while l.indent < *stack.last().unwrap() {
                    stack.pop();
                    toks.push(Tok::Dedent);
                }
                if l.indent != *stack.last().unwrap() {
                    return Err(Error::at(
                        TXT_DEDENT,
                        "a dedent to no enclosing block's column",
                        l.line,
                        l.indent as u32 + 1,
                        &source,
                        file,
                    ));
                }
            }
        }
        toks.push(Tok::Line(k));
    }
    let mut at = 0;
    let out = block(&lines, &toks, &mut at, &source, file)?;
    if at < toks.len() {
        let k = match toks[at] {
            Tok::Line(k) | Tok::Indent(k) => k,
            Tok::Dedent => 0,
        };
        let l = &lines[k];
        return Err(Error::at(
            TXT_ERR,
            "unexpected Token('_INDENT', '')",
            l.line,
            l.col,
            &source,
            file,
        ));
    }
    Ok(out)
}

fn block(
    lines: &[Logical],
    toks: &[Tok],
    at: &mut usize,
    source: &str,
    file: &str,
) -> Result<Vec<Stmt>> {
    let mut out: Vec<Stmt> = Vec::new();
    while *at < toks.len() {
        match toks[*at] {
            Tok::Dedent => {
                *at += 1;
                return Ok(out);
            }
            Tok::Indent(k) => {
                let Some(last) = out.last_mut() else {
                    let l = &lines[k];
                    return Err(Error::at(
                        TXT_ERR,
                        "unexpected Token('_INDENT', '')",
                        l.line,
                        l.col,
                        source,
                        file,
                    ));
                };
                *at += 1;
                last.body = block(lines, toks, at, source, file)?;
            }
            Tok::Line(k) => {
                *at += 1;
                out.push(line(&lines[k], source, file)?);
            }
        }
    }
    Ok(out)
}

/// `text` as a mutated grammar reads it: a call name may stand apart from its
/// bracket (`l1.text/call-spaced`), `<-` is `<` then `-` (`no-left-arrow`).
fn mutated(text: &str) -> String {
    let mut out = text.to_string();
    if on("l1.text/no-left-arrow") {
        out = out.replace("<-", "< -");
    }
    if on("l1.text/call-spaced") {
        let mut s = String::new();
        let chars: Vec<char> = out.chars().collect();
        let mut i = 0;
        while i < chars.len() {
            s.push(chars[i]);
            if chars[i].is_ascii_alphanumeric() || chars[i] == '_' {
                let mut j = i + 1;
                while j < chars.len() && chars[j] == ' ' {
                    j += 1;
                }
                if j > i + 1 && j < chars.len() && chars[j] == '(' {
                    i = j;
                    continue;
                }
            }
            i += 1;
        }
        out = s;
    }
    out
}

fn line(l: &Logical, source: &str, file: &str) -> Result<Stmt> {
    let text = mutated(&l.text);
    let mut st = read(&text).map_err(|at| {
        let (line, col) = match at {
            LineColLocation::Pos((_, c)) => (l.line, c as u32),
            LineColLocation::Span((_, c), _) => (l.line, c as u32),
        };
        let col = col + on("l1.text/col-off") as u32;
        let at = l.text.chars().nth(col as usize - 1);
        let message = match at {
            Some(c) if !c.is_whitespace() => format!("unexpected character {c:?}"),
            _ => "unexpected input".to_string(),
        };
        Error::at(TXT_ERR, message, line, col, source, file)
    })?;
    st.line = l.line;
    st.col = l.col;
    if st.annot.is_some() && on("l1.text/no-annotation") {
        return Err(Error::at(
            TXT_ERR,
            "unexpected ':'",
            l.line,
            l.col,
            source,
            file,
        ));
    }
    if on("l1.text/no-commas") {
        st.commas = false;
    }
    Ok(st)
}
