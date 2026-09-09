// CHECK-LABEL: func.func @add_zero
// CHECK-NEXT: return %arg0 : i32
module {
  func.func @add_zero(%arg0: i32) -> i32 {
    %zero = arith.constant 0 : i32
    %result = arith.addi %arg0, %zero : i32
    return %result : i32
  }
}
