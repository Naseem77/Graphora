"""Coverage: the parser must find every function, method, and class.

Each fixture lists every definition in the source; the parser must return
exactly that set (as (kind, qualified name)), with no extras such as type
usages counted as definitions.
"""

import ast

import pytest

from graphora.parser import parse_code_file

PYTHON = '''\
import functools

def top(x):
    def inner(y):
        return y
    return inner(x)

async def fetch():
    return 1

square = lambda v: v * v

class Base:
    def __init__(self):
        self.ready = True

    @property
    def name(self):
        return "base"

    @staticmethod
    def build():
        return Base()

    class Meta:
        def describe(self):
            return "meta"

@functools.lru_cache
def cached():
    return 2
'''
PYTHON_EXPECTED = {
    ("Function", "top"), ("Function", "top.inner"), ("Function", "fetch"), ("Function", "square"),
    ("Class", "Base"), ("Function", "Base.__init__"), ("Function", "Base.name"), ("Function", "Base.build"),
    ("Class", "Base.Meta"), ("Function", "Base.Meta.describe"), ("Function", "cached"),
}

JAVASCRIPT = '''\
import { thing } from "./thing";

function plain(a) { return a; }
function* gen() { yield 1; }
const arrow = (x) => x * 2;
const expr = function (y) { return y; };
let asyncArrow = async () => { await plain(1); };

export default class Widget extends Base {
  constructor(props) { super(props); }
  render() { return plain(this.props); }
  static create() { return new Widget(); }
  handleClick = (event) => { this.render(); };
  get label() { return "w"; }
}

const api = {
  load() { return 1; },
  save: function () { return 2; },
  remove: () => 3,
};
'''
JAVASCRIPT_EXPECTED = {
    ("Function", "plain"), ("Function", "gen"), ("Function", "arrow"), ("Function", "expr"),
    ("Function", "asyncArrow"), ("Class", "Widget"), ("Function", "Widget.constructor"),
    ("Function", "Widget.render"), ("Function", "Widget.create"), ("Function", "Widget.handleClick"),
    ("Function", "Widget.label"), ("Function", "api.load"), ("Function", "api.save"),
    ("Function", "api.remove"),
}

TYPESCRIPT = '''\
export interface Shape { area(): number; }
export type Id = string;
export enum Color { Red, Green }

export abstract class Animal {
  abstract speak(): string;
  move(distance: number): void { log(distance); }
}

export class Dog extends Animal implements Shape {
  private bark = (): string => "woof";
  speak(): string { return this.bark(); }
  area(): number { return 1; }
}

export const greet = (name: string): string => `hi ${name}`;
export function log(value: unknown): void {}
'''
TYPESCRIPT_EXPECTED = {
    ("Class", "Shape"), ("Class", "Color"), ("Class", "Animal"), ("Function", "Animal.move"),
    ("Class", "Dog"), ("Function", "Dog.bark"), ("Function", "Dog.speak"), ("Function", "Dog.area"),
    ("Function", "greet"), ("Function", "log"),
}

TSX = '''\
import React from "react";

export function App({ title }: { title: string }) {
  return <div onClick={() => track(title)}>{title}</div>;
}

export const Card = ({ body }: { body: string }) => <section>{format(body)}</section>;

function track(name: string) {}
function format(text: string) { return text; }
'''
TSX_EXPECTED = {("Function", "App"), ("Function", "Card"), ("Function", "track"), ("Function", "format")}

GO = '''\
package shapes

import "fmt"

type Shape interface { Area() float64 }

type Rect struct { W, H float64 }

type Celsius float64

func (r Rect) Area() float64 { return r.W * r.H }

func (r *Rect) Scale(f float64) { r.W *= f }

func New(w, h float64) *Rect { return &Rect{w, h} }

func main() { fmt.Println(New(1, 2).Area()) }
'''
GO_EXPECTED = {
    ("Class", "Shape"), ("Class", "Rect"), ("Class", "Celsius"), ("Function", "Rect.Area"),
    ("Function", "Rect.Scale"), ("Function", "New"), ("Function", "main"),
}

JAVA = '''\
package app;

import java.util.List;

public class Service {
    private final Repo repo;

    public Service(Repo repo) { this.repo = repo; }

    public List<String> names() { return repo.all(); }

    static class Helper {
        void help() {}
    }
}

interface Repo { List<String> all(); }

enum Level { LOW, HIGH; int weight() { return 1; } }

record Point(int x, int y) {
    Point { }
    int sum() { return x + y; }
}
'''
JAVA_EXPECTED = {
    ("Class", "Service"), ("Function", "Service.Service"), ("Function", "Service.names"),
    ("Class", "Service.Helper"), ("Function", "Service.Helper.help"), ("Class", "Repo"),
    ("Class", "Level"), ("Function", "Level.weight"), ("Class", "Point"),
    ("Function", "Point.Point"), ("Function", "Point.sum"),
}

RUST = '''\
use std::fmt;

pub struct Point { x: i32 }
pub enum Kind { A, B }
pub trait Shape { fn area(&self) -> f64; fn name(&self) -> String { String::new() } }
union Bits { i: u32, f: f32 }

impl Point {
    pub fn new(x: i32) -> Self { Point { x } }
    fn norm(&self) -> i32 { helper(self.x) }
}

impl Shape for Point {
    fn area(&self) -> f64 { 0.0 }
}

fn helper(x: i32) -> i32 { x }

mod inner {
    pub fn nested() {}
}
'''
RUST_EXPECTED = {
    ("Class", "Point"), ("Class", "Kind"), ("Class", "Shape"), ("Function", "Shape.name"),
    ("Class", "Bits"), ("Function", "Point.new"), ("Function", "Point.norm"),
    ("Function", "Point.area"), ("Function", "helper"), ("Function", "inner.nested"),
}

C = '''\
#include <stdio.h>
#include "point.h"

struct point { int x; int y; };

typedef struct { int r; } color_t;

enum mode { ON, OFF };

static int helper(struct point *p) { return p->x; }

int *make(int n) { return 0; }

int main(void) {
    struct point p = {1, 2};
    return helper(&p);
}
'''
C_EXPECTED = {
    ("Class", "point"), ("Class", "color_t"), ("Class", "mode"), ("Function", "helper"),
    ("Function", "make"), ("Function", "main"),
}

CPP = '''\
#include <vector>

namespace geo {

class Shape {
public:
    virtual ~Shape() {}
    virtual double area() const = 0;
};

class Square : public Shape {
public:
    explicit Square(double s) : side(s) {}
    double area() const override { return side * side; }
private:
    double side;
};

struct Pair { int a; int b; };

double total(const std::vector<Shape*>& shapes);

}  // namespace geo

double geo::total(const std::vector<geo::Shape*>& shapes) {
    double sum = 0;
    for (auto* s : shapes) sum += s->area();
    return sum;
}

template <typename T>
T twice(T v) { return v + v; }
'''
CPP_EXPECTED = {
    ("Class", "geo.Shape"), ("Function", "geo.Shape.~Shape"), ("Class", "geo.Square"),
    ("Function", "geo.Square.Square"), ("Function", "geo.Square.area"), ("Class", "geo.Pair"),
    ("Function", "geo.total"), ("Function", "twice"),
}

RUBY = '''\
require "json"

module Billing
  class Invoice
    def initialize(total)
      @total = total
    end

    def self.build
      new(0)
    end

    def paid?
      @total.zero?
    end
  end

  def self.helper; end
end

def top_level; end
'''
RUBY_EXPECTED = {
    ("Class", "Billing"), ("Class", "Billing.Invoice"), ("Function", "Billing.Invoice.initialize"),
    ("Function", "Billing.Invoice.build"), ("Function", "Billing.Invoice.paid?"),
    ("Function", "Billing.helper"), ("Function", "top_level"),
}

PHP = '''\
<?php
namespace App;

use App\\Models\\User;

interface Payable { public function pay(): void; }

trait Loggable { public function log($m) { echo $m; } }

enum Status { case Open; public function label() { return "x"; } }

abstract class Base { abstract protected function run(); }

class Cart extends Base implements Payable {
    use Loggable;
    public function __construct(private User $user) {}
    public function pay(): void { $this->log("paid"); helper(); }
    protected function run() { return static::make(); }
    public static function make() { return new Cart(new User()); }
}

function helper() { return 1; }
'''
PHP_EXPECTED = {
    ("Class", "Payable"), ("Class", "Loggable"), ("Function", "Loggable.log"), ("Class", "Status"),
    ("Function", "Status.label"), ("Class", "Base"), ("Class", "Cart"),
    ("Function", "Cart.__construct"), ("Function", "Cart.pay"), ("Function", "Cart.run"),
    ("Function", "Cart.make"), ("Function", "helper"),
}

CASES = [
    ("pkg/mod.py", PYTHON, PYTHON_EXPECTED),
    ("web/app.js", JAVASCRIPT, JAVASCRIPT_EXPECTED),
    ("web/zoo.ts", TYPESCRIPT, TYPESCRIPT_EXPECTED),
    ("web/App.tsx", TSX, TSX_EXPECTED),
    ("shapes/rect.go", GO, GO_EXPECTED),
    ("app/Service.java", JAVA, JAVA_EXPECTED),
    ("src/point.rs", RUST, RUST_EXPECTED),
    ("src/point.c", C, C_EXPECTED),
    ("src/geo.cpp", CPP, CPP_EXPECTED),
    ("lib/billing.rb", RUBY, RUBY_EXPECTED),
    ("src/Cart.php", PHP, PHP_EXPECTED),
]


@pytest.mark.parametrize("path,source,expected", CASES, ids=[c[0] for c in CASES])
def test_every_definition_is_found(path, source, expected):
    parsed = parse_code_file(path, source)
    assert parsed.parser_used == "tree-sitter"
    found = {(s.kind, s.qualname) for s in parsed.symbols}
    missing = expected - found
    extra = found - expected
    assert not missing and not extra, f"missing={sorted(missing)} extra={sorted(extra)}"


@pytest.mark.parametrize("path,source,expected", CASES, ids=[c[0] for c in CASES])
def test_symbols_have_valid_ranges(path, source, expected):
    lines = source.count("\n") + 1
    for symbol in parse_code_file(path, source).symbols:
        assert 1 <= symbol.line <= symbol.end_line <= lines, symbol


def test_python_coverage_matches_ast_ground_truth():
    """Every def/class the Python AST sees, the graph parser sees too."""
    tree = ast.parse(PYTHON)
    truth = set()

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}{child.name}"
                truth.add((child.lineno, qual))
                visit(child, qual + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    found = {(s.line, s.qualname) for s in parse_code_file("pkg/mod.py", PYTHON).symbols}
    assert truth <= found


def test_python_calls_attribute_to_innermost_function_with_receiver():
    parsed = parse_code_file("pkg/mod.py", PYTHON)
    calls = {(c.caller, c.callee, c.receiver) for c in parsed.calls}
    assert ("top", "inner", "") in calls
    assert ("Base.build", "Base", "") in calls


def test_receivers_are_recorded():
    src = "class A:\n    def f(self):\n        self.g()\n        os.path.join('a')\n        helper()\n"
    calls = {(c.callee, c.receiver) for c in parse_code_file("a.py", src).calls}
    assert ("g", "self") in calls
    assert ("join", "os.path") in calls
    assert ("helper", "") in calls


def test_same_qualname_twice_in_one_file_stays_two_symbols():
    src = "import sys\nif sys.version_info > (3,):\n    def compat():\n        return 1\nelse:\n    def compat():\n        return 2\n"
    symbols = [s for s in parse_code_file("c.py", src).symbols if s.name == "compat"]
    assert [s.line for s in symbols] == [3, 6]


EDGE_CASES = [
    (
        "web/misc.js",
        "function $http(a) { return a; }\n"
        "const $el = () => 1;\n"
        "export default function () { $http(1); }\n"
        "module.exports = function exported() {};\n"
        "exports.foo = function () {};\n"
        "const K = class { m() {} };\n"
        "class A { #priv() { this.#priv(); } }\n",
        {
            ("Function", "$http"), ("Function", "$el"), ("Function", "default"), ("Function", "exported"),
            ("Function", "foo"), ("Class", "K"), ("Function", "K.m"), ("Class", "A"), ("Function", "A.#priv"),
        },
    ),
    ("web/anon.js", "export default class { n() {} }\n", {("Class", "default"), ("Function", "default.n")}),
    ("pkg/uni.py", "class Ñ:\n    def método(self):\n        return 1\n\ndef π():\n    return 3\n",
     {("Class", "Ñ"), ("Function", "Ñ.método"), ("Function", "π")}),
    ("web/uni.js", "function ñandú() {}\nconst π = () => 3;\n", {("Function", "ñandú"), ("Function", "π")}),
    ("src/refs.rs", "struct Foo<T>(T);\ntrait Tr { fn t(&self); }\nimpl<'a> Tr for &'a Foo<i32> { fn t(&self) {} }\n",
     {("Class", "Foo"), ("Class", "Tr"), ("Function", "Foo.t")}),
    ("src/fp.c", "int (*get_handler(void))(int) { return 0; }\n", {("Function", "get_handler")}),
    (
        "src/ops.cpp",
        "struct V { V operator+(const V& o) const { return o; } };\n"
        "bool operator==(const V& a, const V& b) { return true; }\n"
        "template<typename T> T maxv(T a, T b) { return a; }\n"
        "template<> int maxv<int>(int a, int b) { return a; }\n",
        {("Class", "V"), ("Function", "V.operator+"), ("Function", "operator=="), ("Function", "maxv"),
         ("Function", "maxv")},
    ),
    ("src/Anon.php", "<?php\nfunction anon() {}\n$o = new class { function anon() {} };\n", {("Function", "anon")}),
]


@pytest.mark.parametrize("path,source,expected", EDGE_CASES, ids=[c[0] for c in EDGE_CASES])
def test_edge_case_definitions(path, source, expected):
    found = {(s.kind, s.qualname) for s in parse_code_file(path, source).symbols if not s.qualname.startswith("anonymous")}
    assert found == expected, f"missing={sorted(expected - found)} extra={sorted(found - expected)}"


def test_php_anonymous_class_methods_are_not_top_level():
    symbols = parse_code_file("src/Anon.php", "<?php\n$o = new class { function anon() {} };\n").symbols
    assert all(s.parent_kind == "Class" for s in symbols if s.name == "anon")


def test_dollar_names_are_kept_in_calls():
    calls = {(c.callee, c.receiver) for c in parse_code_file("a.js", "function f() { $foo(); $.ajax(); }\n").calls}
    assert ("$foo", "") in calls and ("ajax", "$") in calls
    php = {(c.callee, c.receiver) for c in parse_code_file("a.php", "<?php function f() { $helper(); }\n").calls}
    assert ("helper", "") not in php  # $helper() calls a variable, not function helper


def test_lone_surrogates_do_not_crash():
    parsed = parse_code_file("odd.py", "def ok():\n    return '\ud800'\n")
    assert [s.name for s in parsed.symbols] == ["ok"]


def test_go_generic_composite_literal_keeps_its_type():
    calls = {(c.callee, c.receiver) for c in parse_code_file(
        "p/a.go", "package p\nfunc f() { s := &S[int]{}; s.Do() }\n").calls}
    assert ("Do", "S") in calls
