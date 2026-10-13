`default_nettype none
// behavioral provider: any shape legal under config/dense_memory.json.
module dense_memory_1rw #(
  parameter int DEPTH = 16,
  parameter int WIDTH = 128,
  parameter bit MASKED = 1'b1,
  localparam int LANES = MASKED ? WIDTH / 8 : 1,
  localparam int ADDR_BITS = $clog2(DEPTH)
) (
  input  wire                 clk_i,
  input  wire                 en_i,
  input  wire                 we_i,
  input  wire [ADDR_BITS-1:0] addr_i,
  input  wire [WIDTH-1:0]     wdata_i,
  input  wire [LANES-1:0]     wmask_i,
  output logic [WIDTH-1:0]    rdata_o
);
  dense_memory_1rw_array #(.DEPTH(DEPTH), .WIDTH(WIDTH), .MASKED(MASKED)) storage (.*);
endmodule
`default_nettype wire
