`default_nettype none
package single_lane_pkg;
  import platform_pkg::*;
  typedef enum logic [4:0] {
    OP_ILLEGAL, OP_ALU, OP_LUI, OP_AUIPC, OP_JAL, OP_JALR, OP_BRANCH,
    OP_LOAD, OP_STORE, OP_CSR, OP_ECALL, OP_EBREAK, OP_MRET, OP_FENCE, OP_WFI, OP_FENCE_I
  } operation_e;
  typedef struct packed {
    operation_e op;
    logic [4:0] rs1, rs2, rd;
    logic [31:0] immediate;
    logic [2:0] funct3;
    logic alternate;
    logic immediate_alu;
    logic [11:0] csr;
    logic [4:0] zimm;
  } decoded_t;
  typedef struct packed {
    logic [4:0] rd;
    logic [5:0] destination, stale;
  } rename_entry_t;
  function automatic logic in_region(input logic [31:0] address, base, size);
    return address >= base && address - base < size;
  endfunction
  function automatic logic executable(input logic [31:0] address);
    return in_region(address, BRAM_BASE, BRAM_SIZE) && BRAM_EXECUTE;
  endfunction
  function automatic logic accessible(input logic [31:0] address, input logic write_access);
    return (in_region(address, BRAM_BASE, BRAM_SIZE) && (write_access ? BRAM_WRITE : BRAM_READ))
      || (in_region(address, UART_BASE, UART_SIZE) && (write_access ? UART_WRITE : UART_READ))
      || (in_region(address, GPIO_BASE, GPIO_SIZE) && (write_access ? GPIO_WRITE : GPIO_READ));
  endfunction
  function automatic logic cached(input logic [31:0] address);
    return in_region(address, BRAM_BASE, BRAM_SIZE) && BRAM_CACHEABLE;
  endfunction
  function automatic logic [31:0] byte_mask(input logic [3:0] mask);
    return {{8{mask[3]}}, {8{mask[2]}}, {8{mask[1]}}, {8{mask[0]}}};
  endfunction
endpackage
`default_nettype wire
