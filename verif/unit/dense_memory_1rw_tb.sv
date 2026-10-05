`default_nettype none
// Replay hexadecimal requests and record the read data after each clock edge.
module dense_memory_1rw_tb #(
  parameter int DEPTH = 16,
  parameter int WIDTH = 128,
  parameter bit MASKED = 1'b1
);
  localparam int LANES = MASKED ? WIDTH / 8 : 1;
  logic clk = 1'b0, en, we;
  logic [$clog2(DEPTH)-1:0] addr;
  logic [WIDTH-1:0] wdata, rdata;
  logic [LANES-1:0] wmask;
  string stim_path, out_path;
  int stim, out;

  dense_memory_1rw #(.DEPTH(DEPTH), .WIDTH(WIDTH), .MASKED(MASKED)) dut (
    .clk_i(clk), .en_i(en), .we_i(we), .addr_i(addr), .wdata_i(wdata), .wmask_i(wmask), .rdata_o(rdata));

  initial begin
    if (!$value$plusargs("stim=%s", stim_path) || !$value$plusargs("out=%s", out_path))
      $fatal(1, "need +stim= and +out=");
    stim = $fopen(stim_path, "r");
    out = $fopen(out_path, "w");
    while ($fscanf(stim, "%h %h %h %h %h", en, we, addr, wdata, wmask) == 5) begin
      #1 clk = 1'b1;
      #1 clk = 1'b0;
      $fdisplay(out, "%h", rdata);
    end
    $fclose(out);
    $finish;
  end
endmodule
`default_nettype wire
