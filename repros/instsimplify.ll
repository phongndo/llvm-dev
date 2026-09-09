; Identity simplification for the workspace's opt/FileCheck smoke test.
; Run: just run opt -passes=instsimplify -S repros/instsimplify.ll
; CHECK-LABEL: define i32 @add_zero(
; CHECK-NEXT: ret i32 %x

define i32 @add_zero(i32 %x) {
  %result = add i32 %x, 0
  ret i32 %result
}
